from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import boto3
import botocore.client
import botocore.exceptions
from pydantic import ValidationError

import dstack._internal.core.backends.aws.resources as aws_resources
from dstack._internal import settings
from dstack._internal.core.backends.aws.config import AWSConfig
from dstack._internal.core.backends.base.compute import (
    Compute,
    get_gateway_user_data,
    get_instance_name,
    get_user_data,
    merge_tags,
)
from dstack._internal.core.backends.base.offers import get_catalog_offers
from dstack._internal.core.errors import ComputeError, NoCapacityError, PlacementGroupInUseError
from dstack._internal.core.models.backends.aws import AWSAccessKeyCreds
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.common import CoreModel, is_core_model_instance
from dstack._internal.core.models.gateways import (
    GatewayComputeConfiguration,
    GatewayProvisioningData,
)
from dstack._internal.core.models.instances import (
    InstanceAvailability,
    InstanceConfiguration,
    InstanceOffer,
    InstanceOfferWithAvailability,
    SSHKey,
)
from dstack._internal.core.models.placement import PlacementGroup, PlacementGroupProvisioningData
from dstack._internal.core.models.resources import Memory, Range
from dstack._internal.core.models.runs import Job, JobProvisioningData, Requirements, Run
from dstack._internal.core.models.volumes import (
    Volume,
    VolumeAttachmentData,
    VolumeProvisioningData,
)
from dstack._internal.utils.logging import get_logger

logger = get_logger(__name__)
# gp2 volumes can be 1GB-16TB, dstack AMIs are 100GB
CONFIGURABLE_DISK_SIZE = Range[Memory](min=Memory.parse("100GB"), max=Memory.parse("16TB"))


class AWSGatewayBackendData(CoreModel):
    lb_arn: str
    tg_arn: str
    listener_arn: str


class AWSVolumeBackendData(CoreModel):
    volume_type: str
    iops: int


class AWSCompute(Compute):
    def __init__(self, config: AWSConfig):
        self.config = config
        if is_core_model_instance(config.creds, AWSAccessKeyCreds):
            self.session = boto3.Session(
                aws_access_key_id=config.creds.access_key,
                aws_secret_access_key=config.creds.secret_key,
            )
        else:  # default creds
            self.session = boto3.Session()

    def get_offers(
        self, requirements: Optional[Requirements] = None
    ) -> List[InstanceOfferWithAvailability]:
        filter = _supported_instances
        if requirements and requirements.reservation:
            region_to_reservation = {}
            for region in self.config.regions:
                reservation = aws_resources.get_reservation(
                    ec2_client=self.session.client("ec2", region_name=region),
                    reservation_id=requirements.reservation,
                    instance_count=1,
                )
                if reservation is not None:
                    region_to_reservation[region] = reservation

            def _supported_instances_with_reservation(offer: InstanceOffer) -> bool:
                # Filter: only instance types supported by dstack
                if not _supported_instances(offer):
                    return False
                # Filter: Spot instances can't be used with reservations
                if offer.instance.resources.spot:
                    return False
                region = offer.region
                reservation = region_to_reservation.get(region)
                # Filter: only instance types matching the capacity reservation
                if not bool(reservation and offer.instance.name == reservation["InstanceType"]):
                    return False
                return True

            filter = _supported_instances_with_reservation

        offers = get_catalog_offers(
            backend=BackendType.AWS,
            locations=self.config.regions,
            requirements=requirements,
            configurable_disk_size=CONFIGURABLE_DISK_SIZE,
            extra_filter=filter,
        )
        regions = set(i.region for i in offers)

        def get_quotas(client: botocore.client.BaseClient) -> Dict[str, int]:
            region_quotas = {}
            for page in client.get_paginator("list_service_quotas").paginate(ServiceCode="ec2"):
                for q in page["Quotas"]:
                    if "On-Demand" in q["QuotaName"]:
                        region_quotas[q["UsageMetric"]["MetricDimensions"]["Class"]] = q["Value"]
            return region_quotas

        quotas = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_region = {}
            for region in regions:
                future = executor.submit(
                    get_quotas, self.session.client("service-quotas", region_name=region)
                )
                future_to_region[future] = region
            for future in as_completed(future_to_region):
                quotas[future_to_region[future]] = future.result()

        availability_offers = []
        for offer in offers:
            availability = InstanceAvailability.UNKNOWN
            if not _has_quota(quotas[offer.region], offer.instance.name):
                availability = InstanceAvailability.NO_QUOTA
            availability_offers.append(
                InstanceOfferWithAvailability(**offer.dict(), availability=availability)
            )
        return availability_offers

    def terminate_instance(
        self, instance_id: str, region: str, backend_data: Optional[str] = None
    ) -> None:
        ec2_client = self.session.client("ec2", region_name=region)
        try:
            ec2_client.terminate_instances(InstanceIds=[instance_id])
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "InvalidInstanceID.NotFound":
                logger.debug("Skipping instance %s termination. Instance not found.", instance_id)
            else:
                raise e

    def create_instance(
        self,
        instance_offer: InstanceOfferWithAvailability,
        instance_config: InstanceConfiguration,
    ) -> JobProvisioningData:
        project_name = instance_config.project_name
        ec2_resource = self.session.resource("ec2", region_name=instance_offer.region)
        ec2_client = self.session.client("ec2", region_name=instance_offer.region)
        allocate_public_ip = self.config.allocate_public_ips
        availability_zones = None
        if instance_config.availability_zone is not None:
            availability_zones = [instance_config.availability_zone]

        tags = {
            "Name": instance_config.instance_name,
            "owner": "dstack",
            "dstack_project": project_name,
            "dstack_user": instance_config.user,
        }
        tags = merge_tags(tags=tags, backend_tags=self.config.tags)

        disk_size = round(instance_offer.instance.resources.disk.size_mib / 1024)
        max_efa_interfaces = get_maximum_efa_interfaces(
            ec2_client=ec2_client, instance_type=instance_offer.instance.name
        )
        enable_efa = max_efa_interfaces > 0
        try:
            vpc_id, subnet_ids = get_vpc_id_subnet_id_or_error(
                ec2_client=ec2_client,
                config=self.config,
                region=instance_offer.region,
                allocate_public_ip=allocate_public_ip,
                availability_zones=availability_zones,
            )
            subnet_id_to_az_map = aws_resources.get_subnets_availability_zones(
                ec2_client=ec2_client,
                subnet_ids=subnet_ids,
            )
            if instance_config.reservation:
                reservation = aws_resources.get_reservation(
                    ec2_client=ec2_client,
                    reservation_id=instance_config.reservation,
                    instance_count=1,
                )
                if reservation is not None:
                    # Filter out az different from capacity reservation
                    subnet_id_to_az_map = {
                        k: v
                        for k, v in subnet_id_to_az_map.items()
                        if v == reservation["AvailabilityZone"]
                    }
        except botocore.exceptions.ClientError as e:
            logger.warning("Got botocore.exceptions.ClientError: %s", e)
            raise NoCapacityError()
        tried_availability_zones = set()
        for subnet_id, az in subnet_id_to_az_map.items():
            if az in tried_availability_zones:
                continue
            tried_availability_zones.add(az)
            try:
                logger.debug("Trying provisioning %s in %s", instance_offer.instance.name, az)
                image_id, username = aws_resources.get_image_id_and_username(
                    ec2_client=ec2_client,
                    cuda=len(instance_offer.instance.resources.gpus) > 0,
                    image_config=self.config.os_images,
                )
                response = ec2_resource.create_instances(
                    **aws_resources.create_instances_struct(
                        disk_size=disk_size,
                        image_id=image_id,
                        instance_type=instance_offer.instance.name,
                        iam_instance_profile_arn=None,
                        user_data=get_user_data(authorized_keys=instance_config.get_public_keys()),
                        tags=aws_resources.make_tags(tags),
                        security_group_id=aws_resources.create_security_group(
                            ec2_client=ec2_client,
                            project_id=project_name,
                            vpc_id=vpc_id,
                        ),
                        spot=instance_offer.instance.resources.spot,
                        subnet_id=subnet_id,
                        allocate_public_ip=allocate_public_ip,
                        placement_group_name=instance_config.placement_group_name,
                        enable_efa=enable_efa,
                        max_efa_interfaces=max_efa_interfaces,
                        reservation_id=instance_config.reservation,
                    )
                )

                # TODO create EFA network interfaces and attach to the node. Wait until node is running.
                #   Maybe should be created before the node and attached on start?

                instance = response[0]
                instance.wait_until_running()
                instance.reload()  # populate instance.public_ip_address
                if instance_offer.instance.resources.spot:  # it will not terminate the instance
                    ec2_client.cancel_spot_instance_requests(
                        SpotInstanceRequestIds=[instance.spot_instance_request_id]
                    )
                hostname = _get_instance_ip(instance, allocate_public_ip)
                return JobProvisioningData(
                    backend=instance_offer.backend,
                    instance_type=instance_offer.instance,
                    instance_id=instance.instance_id,
                    public_ip_enabled=allocate_public_ip,
                    hostname=hostname,
                    internal_ip=instance.private_ip_address,
                    region=instance_offer.region,
                    availability_zone=az,
                    reservation=instance.capacity_reservation_id,
                    price=instance_offer.price,
                    username=username,
                    ssh_port=22,
                    dockerized=True,  # because `dstack-shim docker` is used
                    ssh_proxy=None,
                    backend_data=None,
                )
            except botocore.exceptions.ClientError as e:
                logger.warning("Got botocore.exceptions.ClientError: %s", e)
                continue
        raise NoCapacityError()

    def run_job(
        self,
        run: Run,
        job: Job,
        instance_offer: InstanceOfferWithAvailability,
        project_ssh_public_key: str,
        project_ssh_private_key: str,
        volumes: List[Volume],
    ) -> JobProvisioningData:
        instance_config = InstanceConfiguration(
            project_name=run.project_name,
            instance_name=get_instance_name(run, job),  # TODO: generate name
            ssh_keys=[
                SSHKey(public=project_ssh_public_key.strip()),
            ],
            job_docker_config=None,
            user=run.user,
            reservation=run.run_spec.configuration.reservation,
        )
        if len(volumes) > 0:
            volume = volumes[0]
            if (
                volume.provisioning_data is not None
                and volume.provisioning_data.availability_zone is not None
            ):
                instance_config.availability_zone = volume.provisioning_data.availability_zone
        return self.create_instance(instance_offer, instance_config)

    def create_placement_group(
        self,
        placement_group: PlacementGroup,
    ) -> PlacementGroupProvisioningData:
        ec2_client = self.session.client("ec2", region_name=placement_group.configuration.region)
        logger.debug("Creating placement group %s...", placement_group.name)
        ec2_client.create_placement_group(
            GroupName=placement_group.name,
            Strategy=placement_group.configuration.placement_strategy.value,
        )
        logger.debug("Created placement group %s", placement_group.name)
        return PlacementGroupProvisioningData(
            backend=BackendType.AWS,
            backend_data=None,
        )

    def delete_placement_group(
        self,
        placement_group: PlacementGroup,
    ):
        ec2_client = self.session.client("ec2", region_name=placement_group.configuration.region)
        logger.debug("Deleting placement group %s...", placement_group.name)
        try:
            ec2_client.delete_placement_group(GroupName=placement_group.name)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "InvalidPlacementGroup.Unknown":
                logger.debug("Placement group %s not found", placement_group.name)
                return
            elif e.response["Error"]["Code"] == "InvalidPlacementGroup.InUse":
                logger.debug("Placement group %s is in use", placement_group.name)
                raise PlacementGroupInUseError()
            else:
                raise e
        logger.debug("Deleted placement group %s", placement_group.name)

    def create_gateway(
        self,
        configuration: GatewayComputeConfiguration,
    ) -> GatewayProvisioningData:
        ec2_resource = self.session.resource("ec2", region_name=configuration.region)
        ec2_client = self.session.client("ec2", region_name=configuration.region)

        tags = {
            "Name": configuration.instance_name,
            "owner": "dstack",
            "dstack_project": configuration.project_name,
        }
        if settings.DSTACK_VERSION is not None:
            tags["dstack_version"] = settings.DSTACK_VERSION
        tags = merge_tags(tags=tags, backend_tags=self.config.tags)
        tags = aws_resources.make_tags(tags)

        vpc_id, subnets_ids = get_vpc_id_subnet_id_or_error(
            ec2_client=ec2_client,
            config=self.config,
            region=configuration.region,
            allocate_public_ip=configuration.public_ip,
        )
        subnet_id = subnets_ids[0]
        availability_zone = aws_resources.get_availability_zone_by_subnet_id(
            ec2_client=ec2_client,
            subnet_id=subnet_id,
        )
        security_group_id = aws_resources.create_gateway_security_group(
            ec2_client=ec2_client,
            project_id=configuration.project_name,
            vpc_id=vpc_id,
        )
        response = ec2_resource.create_instances(
            **aws_resources.create_instances_struct(
                disk_size=10,
                image_id=aws_resources.get_gateway_image_id(ec2_client),
                instance_type="t2.micro",
                iam_instance_profile_arn=None,
                user_data=get_gateway_user_data(configuration.ssh_key_pub),
                tags=tags,
                security_group_id=security_group_id,
                spot=False,
                subnet_id=subnet_id,
                allocate_public_ip=configuration.public_ip,
            )
        )
        instance = response[0]
        instance.wait_until_running()
        instance.reload()  # populate instance.public_ip_address
        if configuration.certificate is None or configuration.certificate.type != "acm":
            ip_address = _get_instance_ip(instance, configuration.public_ip)
            return GatewayProvisioningData(
                instance_id=instance.instance_id,
                region=configuration.region,
                availability_zone=availability_zone,
                ip_address=ip_address,
            )

        elb_client = self.session.client("elbv2", region_name=configuration.region)

        if len(subnets_ids) < 2:
            raise ComputeError(
                "Deploying gateway with ACM certificate requires at least two subnets in different AZs"
            )

        logger.debug("Creating ALB for gateway %s...", configuration.instance_name)
        response = elb_client.create_load_balancer(
            Name=f"{configuration.instance_name}-lb",
            Subnets=subnets_ids,
            SecurityGroups=[security_group_id],
            Scheme="internet-facing" if configuration.public_ip else "internal",
            Tags=tags,
            Type="application",
            IpAddressType="ipv4",
        )
        lb = response["LoadBalancers"][0]
        lb_arn = lb["LoadBalancerArn"]
        lb_dns_name = lb["DNSName"]
        logger.debug("Created ALB for gateway %s.", configuration.instance_name)

        logger.debug("Creating Target Group for gateway %s...", configuration.instance_name)
        response = elb_client.create_target_group(
            Name=f"{configuration.instance_name}-tg",
            Protocol="HTTP",
            Port=80,
            VpcId=vpc_id,
            TargetType="instance",
        )
        tg_arn = response["TargetGroups"][0]["TargetGroupArn"]
        logger.debug("Created Target Group for gateway %s", configuration.instance_name)

        logger.debug("Registering ALB target for gateway %s...", configuration.instance_name)
        elb_client.register_targets(
            TargetGroupArn=tg_arn,
            Targets=[
                {"Id": instance.instance_id, "Port": 80},
            ],
        )
        logger.debug("Registered ALB target for gateway %s", configuration.instance_name)

        logger.debug("Creating ALB Listener for gateway %s...", configuration.instance_name)
        response = elb_client.create_listener(
            LoadBalancerArn=lb_arn,
            Protocol="HTTPS",
            Port=443,
            SslPolicy="ELBSecurityPolicy-2016-08",
            Certificates=[
                {"CertificateArn": configuration.certificate.arn},
            ],
            DefaultActions=[
                {
                    "Type": "forward",
                    "TargetGroupArn": tg_arn,
                }
            ],
        )
        listener_arn = response["Listeners"][0]["ListenerArn"]
        logger.debug("Created ALB Listener for gateway %s", configuration.instance_name)

        ip_address = _get_instance_ip(instance, configuration.public_ip)
        return GatewayProvisioningData(
            instance_id=instance.instance_id,
            region=configuration.region,
            ip_address=ip_address,
            hostname=lb_dns_name,
            backend_data=AWSGatewayBackendData(
                lb_arn=lb_arn,
                tg_arn=tg_arn,
                listener_arn=listener_arn,
            ).json(),
        )

    def terminate_gateway(
        self,
        instance_id: str,
        configuration: GatewayComputeConfiguration,
        backend_data: Optional[str] = None,
    ):
        self.terminate_instance(
            instance_id=instance_id,
            region=configuration.region,
            backend_data=None,
        )
        if configuration.certificate is None or configuration.certificate.type != "acm":
            return

        if backend_data is None:
            logger.error(
                "Failed to terminate all gateway %s resources. backend_data is None.",
                configuration.instance_name,
            )
            return

        try:
            backend_data_parsed = AWSGatewayBackendData.parse_raw(backend_data)
        except ValidationError:
            logger.exception(
                "Failed to terminate all gateway %s resources. backend_data parsing error.",
                configuration.instance_name,
            )

        elb_client = self.session.client("elbv2", region_name=configuration.region)

        logger.debug("Deleting ALB resources for gateway %s...", configuration.instance_name)
        elb_client.delete_listener(ListenerArn=backend_data_parsed.listener_arn)
        elb_client.delete_target_group(TargetGroupArn=backend_data_parsed.tg_arn)
        elb_client.delete_load_balancer(LoadBalancerArn=backend_data_parsed.lb_arn)
        logger.debug("Deleted ALB resources for gateway %s", configuration.instance_name)

    def register_volume(self, volume: Volume) -> VolumeProvisioningData:
        ec2_client = self.session.client("ec2", region_name=volume.configuration.region)

        logger.debug("Requesting EBS volume %s", volume.configuration.volume_id)
        try:
            response = ec2_client.describe_volumes(VolumeIds=[volume.configuration.volume_id])
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "InvalidParameterValue":
                raise ComputeError(f"Bad volume id: {volume.configuration.volume_id}")
            else:
                raise e
        response_volumes = response["Volumes"]
        if len(response_volumes) == 0:
            raise ComputeError(f"Volume {volume.configuration.name} not found")
        logger.debug("Found EBS volume %s", volume.configuration.volume_id)

        response_volume = response_volumes[0]
        return VolumeProvisioningData(
            volume_id=response_volume["VolumeId"],
            size_gb=response_volume["Size"],
            availability_zone=response_volume["AvailabilityZone"],
            backend_data=AWSVolumeBackendData(
                volume_type=response_volume["VolumeType"],
                iops=response_volume["Iops"],
            ).json(),
        )

    def create_volume(self, volume: Volume) -> VolumeProvisioningData:
        ec2_client = self.session.client("ec2", region_name=volume.configuration.region)

        tags = {
            "Name": volume.configuration.name,
            "owner": "dstack",
            "dstack_user": volume.user,
            "dstack_project": volume.project_name,
        }
        tags = merge_tags(tags=tags, backend_tags=self.config.tags)

        zone = aws_resources.get_availability_zone(
            ec2_client=ec2_client, region=volume.configuration.region
        )
        if zone is None:
            raise ComputeError(
                f"Failed to find availability zone in region {volume.configuration.region}"
            )

        volume_type = "gp3"

        logger.debug("Creating EBS volume %s", volume.configuration.name)
        response = ec2_client.create_volume(
            Size=volume.configuration.size_gb,
            AvailabilityZone=zone,
            VolumeType=volume_type,
            TagSpecifications=[
                {
                    "ResourceType": "volume",
                    "Tags": aws_resources.make_tags(tags),
                }
            ],
        )
        logger.debug("Created EBS volume %s", volume.configuration.name)

        size = response["Size"]
        iops = response["Iops"]

        return VolumeProvisioningData(
            backend=BackendType.AWS,
            volume_id=response["VolumeId"],
            size_gb=size,
            availability_zone=zone,
            price=_get_volume_price(size=size, iops=iops),
            backend_data=AWSVolumeBackendData(
                volume_type=response["VolumeType"],
                iops=iops,
            ).json(),
        )

    def delete_volume(self, volume: Volume):
        ec2_client = self.session.client("ec2", region_name=volume.configuration.region)

        logger.debug("Deleting EBS volume %s", volume.configuration.name)
        try:
            ec2_client.delete_volume(VolumeId=volume.volume_id)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "InvalidVolume.NotFound":
                pass
            else:
                raise e
        logger.debug("Deleted EBS volume %s", volume.configuration.name)

    def attach_volume(self, volume: Volume, instance_id: str) -> VolumeAttachmentData:
        ec2_client = self.session.client("ec2", region_name=volume.configuration.region)

        device_names = aws_resources.list_available_device_names(
            ec2_client=ec2_client, instance_id=instance_id
        )

        logger.debug("Attaching EBS volume %s to instance %s", volume.volume_id, instance_id)
        for device_name in device_names:
            try:
                ec2_client.attach_volume(
                    VolumeId=volume.volume_id, InstanceId=instance_id, Device=device_name
                )
                break
            except botocore.exceptions.ClientError as e:
                if e.response["Error"]["Code"] == "VolumeInUse":
                    raise ComputeError(f"Failed to attach volume in use: {volume.volume_id}")
                if e.response["Error"]["Code"] == "InvalidVolume.ZoneMismatch":
                    raise ComputeError("Volume zone is different from instance zone")
                if e.response["Error"]["Code"] == "InvalidVolume.NotFound":
                    raise ComputeError("Volume not found")
                if (
                    e.response["Error"]["Code"] == "InvalidParameterValue"
                    and f"Invalid value '{device_name}' for unixDevice"
                    in e.response["Error"]["Message"]
                ):
                    # device name is taken but list API hasn't returned it yet
                    continue
                raise e
        else:
            raise ComputeError(f"Failed to find available device name for volume {volume.name}")

        logger.debug("Attached EBS volume %s to instance %s", volume.volume_id, instance_id)
        return VolumeAttachmentData(device_name=device_name)

    def detach_volume(self, volume: Volume, instance_id: str):
        ec2_client = self.session.client("ec2", region_name=volume.configuration.region)

        logger.debug("Detaching EBS volume %s from instance %s", volume.volume_id, instance_id)
        ec2_client.detach_volume(
            VolumeId=volume.volume_id,
            InstanceId=instance_id,
            Device=volume.attachment_data.device_name,
        )
        logger.debug("Detached EBS volume %s from instance %s", volume.volume_id, instance_id)


def get_maximum_efa_interfaces(ec2_client: botocore.client.BaseClient, instance_type: str) -> int:
    try:
        response = ec2_client.describe_instance_types(
            InstanceTypes=[instance_type],
            Filters=[{"Name": "network-info.efa-supported", "Values": ["true"]}],
        )
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "InvalidInstanceType":
            # "The following supplied instance types do not exist: [<instance_type>]"
            return 0
        raise
    instance_types = response["InstanceTypes"]
    if not instance_types:
        return 0
    return instance_types[0]["NetworkInfo"]["EfaInfo"]["MaximumEfaInterfaces"]


def get_vpc_id_subnet_id_or_error(
    ec2_client: botocore.client.BaseClient,
    config: AWSConfig,
    region: str,
    allocate_public_ip: bool,
    availability_zones: Optional[List[str]] = None,
) -> Tuple[str, List[str]]:
    if config.vpc_ids is not None:
        vpc_id = config.vpc_ids.get(region)
        if vpc_id is not None:
            vpc = aws_resources.get_vpc_by_vpc_id(ec2_client=ec2_client, vpc_id=vpc_id)
            if vpc is None:
                raise ComputeError(f"Failed to find VPC {vpc_id} in region {region}")
            subnets_ids = aws_resources.get_subnets_ids_for_vpc(
                ec2_client=ec2_client,
                vpc_id=vpc_id,
                allocate_public_ip=allocate_public_ip,
                availability_zones=availability_zones,
            )
            if len(subnets_ids) > 0:
                return vpc_id, subnets_ids
            if allocate_public_ip:
                raise ComputeError(f"Failed to find public subnets for VPC {vpc_id}")
            raise ComputeError(
                f"Failed to find private subnets for VPC {vpc_id} with outbound internet access. "
                "Ensure you've setup NAT Gateway, Transit Gateway, or other mechanism "
                "to provide outbound internet access from private subnets."
            )
        if not config.use_default_vpcs:
            raise ComputeError(f"No VPC ID configured for region {region}")

    return _get_vpc_id_subnet_id_by_vpc_name_or_error(
        ec2_client=ec2_client,
        vpc_name=config.vpc_name,
        region=region,
        allocate_public_ip=allocate_public_ip,
        availability_zones=availability_zones,
    )


def _get_vpc_id_subnet_id_by_vpc_name_or_error(
    ec2_client: botocore.client.BaseClient,
    vpc_name: Optional[str],
    region: str,
    allocate_public_ip: bool,
    availability_zones: Optional[List[str]] = None,
) -> Tuple[str, List[str]]:
    if vpc_name is not None:
        vpc_id = aws_resources.get_vpc_id_by_name(
            ec2_client=ec2_client,
            vpc_name=vpc_name,
        )
        if vpc_id is None:
            raise ComputeError(f"No VPC named {vpc_name} in region {region}")
    else:
        vpc_id = aws_resources.get_default_vpc_id(ec2_client=ec2_client)
        if vpc_id is None:
            raise ComputeError(f"No default VPC in region {region}")
    subnets_ids = aws_resources.get_subnets_ids_for_vpc(
        ec2_client=ec2_client,
        vpc_id=vpc_id,
        allocate_public_ip=allocate_public_ip,
        availability_zones=availability_zones,
    )
    if len(subnets_ids) > 0:
        return vpc_id, subnets_ids
    if vpc_name is not None:
        if allocate_public_ip:
            raise ComputeError(
                f"Failed to find public subnets for VPC {vpc_name} in region {region}"
            )
        raise ComputeError(
            f"Failed to find private subnets with NAT for VPC {vpc_name} in region {region}"
        )
    if allocate_public_ip:
        raise ComputeError(f"Failed to find public subnets for default VPC in region {region}")
    raise ComputeError(
        f"Failed to find private subnets with NAT for default VPC in region {region}"
    )


def _has_quota(quotas: Dict[str, int], instance_name: str) -> bool:
    if instance_name.startswith("p"):
        return quotas.get("P/OnDemand", 0) > 0
    if instance_name.startswith("g"):
        return quotas.get("G/OnDemand", 0) > 0
    return quotas.get("Standard/OnDemand", 0) > 0


def _supported_instances(offer: InstanceOffer) -> bool:
    for family in [
        "t2.small",
        "c5.",
        "m5.",
        "g4dn.",
        "g5.",
        "g6.",
        "g6e.",
        "gr6.",
        "p3.",
        "p4d.",
        "p4de.",
        "p5.",
        "c6in.",
    ]:
        if offer.instance.name.startswith(family):
            return True
    return False


def _get_instance_ip(instance: Any, public_ip: bool) -> str:
    if public_ip:
        return instance.public_ip_address
    return instance.private_ip_address


def _get_volume_price(size: int, iops: int) -> float:
    # https://aws.amazon.com/ebs/pricing/
    return size * 0.08 + (iops - 3000) * 0.005
