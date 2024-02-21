import time
import uuid
from typing import Dict, List, Optional, Set

from ray.core.generated.instance_manager_pb2 import Instance


class InstanceUtil:
    """
    A helper class to group updates and operations on an Instance object defined
    in instance_manager.proto
    """

    # Memoized reachable from sets, where the key is the instance status, and
    # the value is the set of instance status that is reachable from the key
    # instance status.
    _reachable_from: Optional[
        Dict["Instance.InstanceStatus", Set["Instance.InstanceStatus"]]
    ] = None

    @staticmethod
    def new_instance(
        instance_id: str,
        instance_type: str,
        status: Instance.InstanceStatus,
        details: str = "",
    ) -> Instance:
        """
        Returns a new instance with the given status.

        Args:
            instance_id: The instance id.
            instance_type: The instance type.
            status: The status of the new instance.
            details: The details of the status transition.
        """
        instance = Instance()
        instance.version = 0  # it will be populated by the underlying storage.
        instance.instance_id = instance_id
        instance.instance_type = instance_type
        instance.status = status
        InstanceUtil._record_status_transition(instance, status, details)
        return instance

    @staticmethod
    def random_instance_id() -> str:
        """
        Returns a random instance id.
        """
        return str(uuid.uuid4())

    @staticmethod
    def is_cloud_instance_allocated(instance_status: Instance.InstanceStatus) -> bool:
        """
        Returns True if the instance is in a status where there could exist
        a cloud instance allocated by the cloud provider.
        """
        assert instance_status != Instance.UNKNOWN
        return instance_status in {
            Instance.ALLOCATED,
            Instance.RAY_INSTALLING,
            Instance.RAY_RUNNING,
            Instance.RAY_STOPPING,
            Instance.RAY_STOPPED,
            Instance.TERMINATING,
            Instance.RAY_INSTALL_FAILED,
            Instance.TERMINATION_FAILED,
        }

    @staticmethod
    def is_ray_running_reachable(instance_status: Instance.InstanceStatus) -> bool:
        """
        Returns True if the instance is in a status where it may transition
        to RAY_RUNNING status.
        """
        return Instance.RAY_RUNNING in InstanceUtil.get_reachable_statuses(
            instance_status
        )

    @staticmethod
    def set_status(
        instance: Instance,
        new_instance_status: Instance.InstanceStatus,
        details: str = "",
    ) -> bool:
        """Transitions the instance to the new state.

        Args:
            instance: The instance to update.
            new_instance_status: The new status to transition to.
            details: The details of the transition.

        Returns:
            True if the status transition is successful, False otherwise.
        """
        if (
            new_instance_status
            not in InstanceUtil.get_valid_transitions()[instance.status]
        ):
            return False
        instance.status = new_instance_status
        InstanceUtil._record_status_transition(instance, new_instance_status, details)
        return True

    @staticmethod
    def _record_status_transition(
        instance: Instance, status: Instance.InstanceStatus, details: str
    ):
        """Records the status transition.

        Args:
            instance: The instance to update.
            status: The new status to transition to.
        """
        now_ns = time.time_ns()
        instance.status_history.append(
            Instance.StatusHistory(
                instance_status=status,
                timestamp_ns=now_ns,
                details=details,
            )
        )

    @staticmethod
    def get_valid_transitions() -> Dict[
        "Instance.InstanceStatus", Set["Instance.InstanceStatus"]
    ]:
        return {
            # This is the initial status of a new instance.
            Instance.QUEUED: {
                # Cloud provider requested to launch a node for the instance.
                # This happens when the a launch request is made to the node provider.
                Instance.REQUESTED
            },
            # When in this status, a launch request to the node provider is made.
            Instance.REQUESTED: {
                # Cloud provider allocated a cloud instance for the instance.
                # This happens when the cloud instance first appears in the list of
                # running cloud instances from the cloud instance provider.
                Instance.ALLOCATED,
                # Retry the allocation, become queueing again.
                Instance.QUEUED,
                # Cloud provider fails to allocate one. Either as a timeout or
                # the launch request fails immediately.
                Instance.ALLOCATION_FAILED,
            },
            # When in this status, the cloud instance is allocated and running. This
            # happens when the cloud instance is present in node provider's list of
            # running cloud instances.
            Instance.ALLOCATED: {
                # Ray needs to be install and launch on the provisioned cloud instance.
                # This happens when the cloud instance is allocated, and the autoscaler
                # is responsible for installing and launching ray on the cloud instance.
                # For node provider that manages the ray installation and launching,
                # this state is skipped.
                Instance.RAY_INSTALLING,
                # Ray is already installed on the provisioned cloud
                # instance. It could be any valid ray status.
                Instance.RAY_RUNNING,
                Instance.RAY_STOPPING,
                Instance.RAY_STOPPED,
                # Instance is requested to be stopped, e.g. instance leaked: no matching
                # Instance with the same type is found in the autoscaler's state.
                Instance.TERMINATING,
                # cloud instance somehow failed.
                Instance.TERMINATED,
            },
            # Ray process is being installed and started on the cloud instance.
            # This status is skipped for node provider that manages the ray
            # installation and launching. (e.g. Ray-on-Spark)
            Instance.RAY_INSTALLING: {
                # Ray installed and launched successfully, reported by the ray cluster.
                # Similar to the Instance.ALLOCATED -> Instance.RAY_RUNNING transition,
                # where the ray process is managed by the node provider.
                Instance.RAY_RUNNING,
                # Ray installation failed. This happens when the ray process failed to
                # be installed and started on the cloud instance.
                Instance.RAY_INSTALL_FAILED,
                # Wen the ray node is reported as stopped by the ray cluster.
                # This could happen that the ray process was stopped quickly after start
                # such that a ray running node  wasn't discovered and the RAY_RUNNING
                # transition was skipped.
                Instance.RAY_STOPPED,
                # cloud instance somehow failed during the installation process.
                Instance.TERMINATED,
            },
            # Ray process is installed and running on the cloud instance. When in this
            # status, a ray node must be present in the ray cluster.
            Instance.RAY_RUNNING: {
                # Ray is requested to be stopped to the ray cluster,
                # e.g. idle termination.
                Instance.RAY_STOPPING,
                # Ray is already stopped, as reported by the ray cluster.
                Instance.RAY_STOPPED,
                # cloud instance somehow failed.
                Instance.TERMINATED,
            },
            # When in this status, the ray process is requested to be stopped to the
            # ray cluster, but not yet present in the dead ray node list reported by
            # the ray cluster.
            Instance.RAY_STOPPING: {
                # Ray is stopped, and the ray node is present in the dead ray node list
                # reported by the ray cluster.
                Instance.RAY_STOPPED,
                # cloud instance somehow failed.
                Instance.TERMINATED,
            },
            # When in this status, the ray process is stopped, and the ray node is
            # present in the dead ray node list reported by the ray cluster.
            Instance.RAY_STOPPED: {
                # cloud instance is requested to be stopped.
                Instance.TERMINATING,
                # cloud instance somehow failed.
                Instance.TERMINATED,
            },
            # When in this status, the cloud instance is requested to be stopped to
            # the node provider.
            Instance.TERMINATING: {
                # When a cloud instance no longer appears in the list of running cloud
                # instances from the node provider.
                Instance.TERMINATED,
                # When the cloud instance failed to be terminated.
                Instance.TERMINATION_FAILED,
            },
            # When in this status, the cloud instance failed to be terminated by the
            # node provider. We will keep retrying.
            Instance.TERMINATION_FAILED: {
                # Retry the termination, become terminating again.
                Instance.TERMINATING,
            },
            # Whenever a cloud instance disappears from the list of running cloud
            # instances from the node provider, the instance is marked as stopped. Since
            # we guarantee 1:1 mapping of a Instance to a cloud instance, this is a
            # terminal state.
            Instance.TERMINATED: set(),  # Terminal state.
            # When in this status, the cloud instance failed to be allocated by the
            # node provider.
            Instance.ALLOCATION_FAILED: set(),  # Terminal state.
            Instance.RAY_INSTALL_FAILED: {
                # Autoscaler requests to shutdown the instance when ray install failed.
                Instance.TERMINATING,
                # cloud instance somehow failed.
                Instance.TERMINATED,
            },
            # Initial state before the instance is created. Should never be used.
            Instance.UNKNOWN: set(),
        }

    @staticmethod
    def get_status_transition_times_ns(
        instance: Instance,
        select_instance_status: Optional["Instance.InstanceStatus"] = None,
    ) -> List[int]:
        """
        Returns a list of timestamps of the instance status update.

        Args:
            instance: The instance.
            instance_status: The status to search for. If None, returns all
                status updates timestamps.

        Returns:
            The list of timestamps of the instance status updates.
        """
        ts_list = []
        for status_update in instance.status_history:
            if (
                select_instance_status
                and status_update.instance_status != select_instance_status
            ):
                continue
            ts_list.append(status_update.timestamp_ns)

        return ts_list

    @classmethod
    def get_reachable_statuses(
        cls,
        instance_status: Instance.InstanceStatus,
    ) -> Set["Instance.InstanceStatus"]:
        """
        Returns the set of instance status that is reachable from the given
        instance status following the status transitions.
        This method is memoized.
        Args:
            instance_status: The instance status to start from.
        Returns:
            The set of instance status that is reachable from the given instance
            status.
        """
        if cls._reachable_from is None:
            cls._compute_reachable()
        return cls._reachable_from[instance_status]

    @classmethod
    def _compute_reachable(cls):
        """
        Computes and memorize the from status sets for each status machine with
        a DFS search.
        """
        valid_transitions = cls.get_valid_transitions()

        def dfs(graph, start, visited):
            """
            Regular DFS algorithm to find all reachable nodes from a given node.
            """
            for next_node in graph[start]:
                if next_node not in visited:
                    # We delay adding the visited set here so we could capture
                    # the self loop.
                    visited.add(next_node)
                    dfs(graph, next_node, visited)
            return visited

        # Initialize the graphs
        cls._reachable_from = {}
        for status in Instance.InstanceStatus.values():
            # All nodes reachable from 'start'
            visited = set()
            cls._reachable_from[status] = dfs(valid_transitions, status, visited)