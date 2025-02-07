import shlex
import sys
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Union

from cachetools import TTLCache, cached

import dstack.version as version
from dstack._internal.core.errors import DockerRegistryError, ServerClientError
from dstack._internal.core.models.common import RegistryAuth, is_core_model_instance
from dstack._internal.core.models.configurations import (
    PortMapping,
    PythonVersion,
    RunConfigurationType,
)
from dstack._internal.core.models.profiles import DEFAULT_STOP_DURATION, SpotPolicy
from dstack._internal.core.models.runs import (
    AppSpec,
    JobSpec,
    Requirements,
    Retry,
    RunSpec,
)
from dstack._internal.core.models.unix import UnixUser
from dstack._internal.core.models.volumes import MountPoint, VolumeMountPoint
from dstack._internal.core.services.profiles import get_retry
from dstack._internal.core.services.ssh.ports import filter_reserved_ports
from dstack._internal.server.services.docker import ImageConfig, get_image_config
from dstack._internal.utils.common import run_async
from dstack._internal.utils.interpolator import InterpolatorError, VariablesInterpolator


def get_default_python_verison() -> str:
    version_info = sys.version_info
    python_version_str = f"{version_info.major}.{version_info.minor}"
    try:
        return PythonVersion(python_version_str).value
    except ValueError:
        raise ServerClientError(
            "Failed to use the system Python version. "
            f"Python {python_version_str} is not supported."
        )


def get_default_image(python_version: str, nvcc: bool = False) -> str:
    suffix = ""
    if nvcc:
        suffix = "-devel"
    return f"dstackai/base:py{python_version}-{version.base_image}-cuda-12.1{suffix}"


class JobConfigurator(ABC):
    TYPE: RunConfigurationType

    _image_config: Optional[ImageConfig] = None

    def __init__(self, run_spec: RunSpec):
        self.run_spec = run_spec

    async def get_job_specs(self, replica_num: int) -> List[JobSpec]:
        job_spec = await self._get_job_spec(replica_num=replica_num, job_num=0, jobs_per_replica=1)
        return [job_spec]

    @abstractmethod
    def _shell_commands(self) -> List[str]:
        pass

    @abstractmethod
    def _default_single_branch(self) -> bool:
        pass

    @abstractmethod
    def _default_max_duration(self) -> Optional[int]:
        pass

    @abstractmethod
    def _spot_policy(self) -> SpotPolicy:
        pass

    @abstractmethod
    def _ports(self) -> List[PortMapping]:
        pass

    async def _get_image_config(self) -> ImageConfig:
        if self._image_config is not None:
            return self._image_config
        image_config = await run_async(
            _get_image_config,
            self._image_name(),
            self.run_spec.configuration.registry_auth,
        )
        self._image_config = image_config
        return image_config

    async def _get_job_spec(
        self,
        replica_num: int,
        job_num: int,
        jobs_per_replica: int,
    ) -> JobSpec:
        job_spec = JobSpec(
            replica_num=replica_num,  # TODO(egor-s): add to env variables in the runner
            job_num=job_num,
            job_name=f"{self.run_spec.run_name}-{job_num}-{replica_num}",
            jobs_per_replica=jobs_per_replica,
            app_specs=self._app_specs(),
            commands=await self._commands(),
            env=self._env(),
            home_dir=self._home_dir(),
            image_name=self._image_name(),
            user=await self._user(),
            privileged=self._privileged(),
            single_branch=self._single_branch(),
            max_duration=self._max_duration(),
            stop_duration=self._stop_duration(),
            registry_auth=self._registry_auth(),
            requirements=self._requirements(),
            retry=self._retry(),
            working_dir=self._working_dir(),
            volumes=self._volumes(job_num),
        )
        return job_spec

    async def _commands(self) -> List[str]:
        if self.run_spec.configuration.entrypoint is not None:  # docker-like format
            entrypoint = shlex.split(self.run_spec.configuration.entrypoint)
            commands = self.run_spec.configuration.commands
        elif self.run_spec.configuration.image is None:  # dstackai/base
            entrypoint = ["/bin/bash", "-i", "-c"]
            commands = [_join_shell_commands(self._shell_commands())]
        elif self._shell_commands():  # custom docker image with shell commands
            entrypoint = ["/bin/sh", "-i", "-c"]
            commands = [_join_shell_commands(self._shell_commands())]
        else:  # custom docker image without commands
            image_config = await self._get_image_config()
            entrypoint = image_config.entrypoint or []
            commands = image_config.cmd or []

        result = entrypoint + commands
        if not result:
            raise ServerClientError(
                "Could not determine what command to run. "
                "Please specify either `commands` or `entrypoint` in your run configuration"
            )

        return result

    def _app_specs(self) -> List[AppSpec]:
        specs = []
        for i, pm in enumerate(filter_reserved_ports(self._ports())):
            specs.append(
                AppSpec(
                    port=pm.container_port,
                    map_to_port=pm.local_port,
                    app_name=f"app_{i}",
                )
            )
        return specs

    def _env(self) -> Dict[str, str]:
        return self.run_spec.configuration.env.as_dict()

    def _home_dir(self) -> Optional[str]:
        return self.run_spec.configuration.home_dir

    def _image_name(self) -> str:
        if self.run_spec.configuration.image is not None:
            return self.run_spec.configuration.image
        return get_default_image(self._python(), nvcc=bool(self.run_spec.configuration.nvcc))

    async def _user(self) -> Optional[UnixUser]:
        user = self.run_spec.configuration.user
        if user is None:
            image_config = await self._get_image_config()
            user = image_config.user
        if user is None:
            return None
        return UnixUser.parse(user)

    def _privileged(self) -> bool:
        return self.run_spec.configuration.privileged

    def _single_branch(self) -> bool:
        if self.run_spec.configuration.single_branch is None:
            return self._default_single_branch()
        return self.run_spec.configuration.single_branch

    def _max_duration(self) -> Optional[int]:
        if self.run_spec.merged_profile.max_duration in [None, True]:
            return self._default_max_duration()
        if self.run_spec.merged_profile.max_duration in ["off", False]:
            return None
        # pydantic validator ensures this is int
        return self.run_spec.merged_profile.max_duration

    def _stop_duration(self) -> Optional[int]:
        if self.run_spec.merged_profile.stop_duration in [None, True]:
            return DEFAULT_STOP_DURATION
        if self.run_spec.merged_profile.stop_duration in ["off", False]:
            return None
        # pydantic validator ensures this is int
        return self.run_spec.merged_profile.stop_duration

    def _registry_auth(self) -> Optional[RegistryAuth]:
        return self.run_spec.configuration.registry_auth

    def _requirements(self) -> Requirements:
        spot_policy = self._spot_policy()
        return Requirements(
            resources=self.run_spec.configuration.resources,
            max_price=self.run_spec.merged_profile.max_price,
            spot=None if spot_policy == SpotPolicy.AUTO else (spot_policy == SpotPolicy.SPOT),
            reservation=self.run_spec.merged_profile.reservation,
        )

    def _retry(self) -> Optional[Retry]:
        return get_retry(self.run_spec.merged_profile)

    def _working_dir(self) -> Optional[str]:
        """
        None means default working directory
        """
        return self.run_spec.working_dir

    def _python(self) -> str:
        if self.run_spec.configuration.python is not None:
            return self.run_spec.configuration.python.value
        return get_default_python_verison()

    def _volumes(self, job_num: int) -> List[MountPoint]:
        return interpolate_job_volumes(self.run_spec.configuration.volumes, job_num)


def interpolate_job_volumes(
    run_volumes: List[Union[MountPoint, str]],
    job_num: int,
) -> List[MountPoint]:
    if len(run_volumes) == 0:
        return []
    interpolator = VariablesInterpolator(
        namespaces={
            "dstack": {
                "job_num": str(job_num),
                "node_rank": str(job_num),  # an alias for job_num
            }
        }
    )
    job_volumes = []
    for mount_point in run_volumes:
        if isinstance(mount_point, str):
            # pydantic validator ensures strings are converted to MountPoint
            continue
        if not is_core_model_instance(mount_point, VolumeMountPoint):
            job_volumes.append(mount_point.copy())
            continue
        if isinstance(mount_point.name, str):
            names = [mount_point.name]
        else:
            names = mount_point.name
        try:
            interpolated_names = [interpolator.interpolate_or_error(n) for n in names]
        except InterpolatorError as e:
            raise ServerClientError(e.args[0])
        job_volumes.append(
            VolumeMountPoint(
                name=interpolated_names,
                path=mount_point.path,
            )
        )
    return job_volumes


def _join_shell_commands(commands: List[str]) -> str:
    for i, cmd in enumerate(commands):
        cmd = cmd.strip()
        if cmd.endswith("&"):  # escape background command
            cmd = "{ %s }" % cmd
        commands[i] = cmd
    return " && ".join(commands)


@cached(TTLCache(maxsize=2048, ttl=80))
def _get_image_config(image: str, registry_auth: Optional[RegistryAuth]) -> ImageConfig:
    try:
        return get_image_config(image, registry_auth).config
    except DockerRegistryError as e:
        raise ServerClientError(
            f"Error pulling configuration for image {image!r} from the docker registry: {e}"
        )
