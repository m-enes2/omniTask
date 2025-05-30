from typing import Dict, List, Any, Callable, Optional, Set
import logging
from datetime import datetime
import time
import asyncio
from ..models.task_result import TaskResult
from ..models.task_group import TaskGroupConfig, TaskGroup
from .task import Task, TaskStatus
from .registry import TaskRegistry

class Workflow:
    """
    A workflow is a collection of tasks that are executed in a specific order based on their dependencies.
    It manages task execution, dependency resolution, and output chaining between tasks.
    """

    def __init__(self, name: str, registry: Optional[TaskRegistry] = None):
        """
        Initialize a new workflow.

        Args:
            name (str): A unique identifier for the workflow
            registry (TaskRegistry, optional): The task registry to use. If not provided, a new one will be created.
        """
        self.name = name
        self.registry = registry or TaskRegistry()
        self.tasks: Dict[str, Task] = {}
        self.task_groups: Dict[str, TaskGroup] = {}
        self.logger = logging.getLogger(f"workflow.{name}")
        self.execution_order: List[str] = []
        self.task_dependencies: Dict[str, Set[str]] = {}
        self.task_dependents: Dict[str, Set[str]] = {}

    def add_task(self, task: Task) -> None:
        """
        Add a task to the workflow.

        Args:
            task (Task): The task to add

        Raises:
            ValueError: If a task with the same name already exists in the workflow
        """
        if task.name in self.tasks:
            raise ValueError(f"Task with name {task.name} already exists in workflow")
        self.tasks[task.name] = task

    def add_task_group(self, name: str, config: TaskGroupConfig) -> None:
        if name in self.task_groups:
            raise ValueError(f"Task group {name} already exists")

        group = TaskGroup(name, config)
        self.task_groups[name] = group

    def register_function(self, func: Callable, name: Optional[str] = None) -> None:
        self.registry.register_function(func, name)

    def create_task(self, task_type: str, name: str, config: Dict[str, Any]) -> Task:
        if name in self.tasks:
            raise ValueError(f"Task {name} already exists")

        task = self.registry.create_task(task_type, name, config)
        self.tasks[name] = task
        return task

    def create_function_task(self, func_name: str, name: str, config: Dict[str, Any] = None) -> Task:
        task = self.registry.create_function_task(func_name, name, config)
        self.add_task(task)
        return task

    def _extract_items_from_output(self, result: TaskResult, path: str) -> List[Any]:
        if not result.success:
            raise ValueError(f"Cannot extract items from failed task result: {result.error}")
        
        current = result.output
        for part in path.split('.'):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                raise ValueError(f"Path {path} not found in task output")
        
        if not isinstance(current, list):
            raise ValueError(f"Expected list at path {path}, got {type(current)}")
        
        return current

    def _prepare_task_config(self, template: Dict[str, Any], item: Any) -> Dict[str, Any]:
        config = {}
        for key, value in template.items():
            if isinstance(value, str) and "${item}" in value:
                config[key] = value.replace("${item}", str(item))
            else:
                config[key] = value
        return config

    async def _execute_task_group(self, group_name: str, parent_result: TaskResult) -> Dict[str, TaskResult]:
        group_config = self.task_groups[group_name]
        items = self._extract_items_from_output(parent_result, group_config.for_each)
        
        tasks = []
        for item in items:
            config = self._prepare_task_config(group_config.config_template, item)
            task = self.create_task(group_config.type, f"{group_name}_{item}", config)
            tasks.append(task)
        
        results = {}
        semaphore = asyncio.Semaphore(group_config.max_concurrent)
        
        async def execute_task(task: Task) -> None:
            async with semaphore:
                try:
                    result = await task.execute_with_timeout()
                    results[task.name] = result
                except Exception as e:
                    results[task.name] = TaskResult(success=False, output={}, error=e)
        
        await asyncio.gather(
            *(execute_task(task) for task in tasks),
            return_exceptions=True
        )
        return results

    def _extract_from_path(self, data, path):
        parts = path.split(".")
        current = data
        for part in parts:
            if part == "output" and isinstance(current, dict) and "output" in current:
                current = current["output"]
            elif part in current:
                current = current[part]
            else:
                if part in current:
                    current = current[part]
                else:
                    raise KeyError(f"Path part '{part}' not found in {current}")
        return current

    def _build_dependency_graph(self) -> None:
        self.task_dependencies = {}
        self.task_dependents = {}
        
        for task_name, task in self.tasks.items():
            self.task_dependencies[task_name] = set(task.task_dependencies)
            for dep in task.task_dependencies:
                if dep not in self.task_dependents:
                    self.task_dependents[dep] = set()
                self.task_dependents[dep].add(task_name)

    def _get_ready_tasks(self, completed_tasks: Set[str]) -> Set[str]:
        ready = set()
        for task_name in self.tasks:
            if task_name not in completed_tasks and task_name not in ready:
                deps = self.task_dependencies.get(task_name, set())
                if all(dep in completed_tasks for dep in deps):
                    ready.add(task_name)
        return ready

    async def _execute_task(self, task_name: str, results: Dict[str, TaskResult]) -> TaskResult:
        task = self.tasks[task_name]
        task.dependency_outputs = {
            prev_task: results[prev_task].output
            for prev_task in self.task_dependencies[task_name]
        }
        task.dependency_order = list(self.task_dependencies[task_name])
        
        result = await task.execute_with_timeout()
        results[task_name] = result
        
        if not result.success:
            error_type = "timeout" if isinstance(result.error, TimeoutError) else "error"
            self.logger.error(f"Task {task_name} failed with {error_type}: {result.error}")
        
        return result

    async def run(self) -> Dict[str, TaskResult]:
        """
        Run the entire workflow, executing all tasks in the correct order based on their dependencies.

        Returns:
            Dict[str, TaskResult]: A dictionary mapping task names to their execution results

        Note:
            - Tasks are executed in topological order based on their dependencies
            - If a task fails, the workflow stops and returns the results up to that point
            - Each task's output is made available to its dependent tasks
        """
        results = {}
        completed_tasks = set()
        self._build_dependency_graph()
        
        while True:
            ready_tasks = self._get_ready_tasks(completed_tasks)
            if not ready_tasks:
                break
                
            tasks_to_execute = []
            for task_name in ready_tasks:
                if task_name in self.tasks:
                    tasks_to_execute.append(task_name)
            
            if not tasks_to_execute:
                break
                
            self.logger.info(f"Executing {len(tasks_to_execute)} tasks concurrently: {tasks_to_execute}")
            
            task_results = await asyncio.gather(
                *(self._execute_task(task_name, results) for task_name in tasks_to_execute),
                return_exceptions=True
            )
            
            for task_name, result in zip(tasks_to_execute, task_results):
                if isinstance(result, Exception):
                    self.logger.error(f"Task {task_name} failed with error: {result}")
                    results[task_name] = TaskResult(success=False, output={}, error=result)
                completed_tasks.add(task_name)
                
                if not results[task_name].success:
                    self.logger.error(f"Workflow stopped due to task {task_name} failure")
                    return results
                
                for group_name, group in self.task_groups.items():
                    if any(dep == task_name for dep in group.config.for_each.split('.')):
                        self.logger.info(f"Executing task group {group_name} for task {task_name}")
                        
                        path_parts = group.config.for_each.split('.')
                        current = results[task_name].output
                        
                        for part in path_parts[1:]:
                            if isinstance(current, dict) and part in current:
                                current = current[part]
                            else:
                                raise ValueError(f"Path {group.config.for_each} not found in task output")
                        
                        if not isinstance(current, list):
                            raise ValueError(f"Expected list at path {group.config.for_each}, got {type(current)}")
                        
                        group.create_tasks(self.registry, current)
                        group_result = await group.execute()
                        results[group_name] = group_result
                        
                        for next_task_name in self.task_dependents.get(group_name, set()):
                            if next_task_name in self.tasks:
                                next_task = self.tasks[next_task_name]
                                next_task.dependency_outputs[group_name] = group_result.output
                                if group_name not in next_task.dependency_order:
                                    next_task.dependency_order.append(group_name)
        
        return results

    def get_task(self, name: str) -> Task:
        if name not in self.tasks:
            raise ValueError(f"Task {name} not found")
        return self.tasks[name]

    def get_task_group(self, name: str) -> TaskGroup:
        if name not in self.task_groups:
            raise ValueError(f"Task group {name} not found")
        return self.task_groups[name]

    def get_all_tasks(self) -> List[Task]:
        return list(self.tasks.values())

    def get_all_task_groups(self) -> List[TaskGroup]:
        return list(self.task_groups.values())

    def execute(self) -> None:
        for task in self.tasks.values():
            task.execute()

        for group in self.task_groups.values():
            group.execute()

    def get_task_output(self, task_name: str) -> Any:
        if task_name in self.tasks:
            return self.tasks[task_name].get_output()
        elif task_name in self.task_groups:
            return self.task_groups[task_name].get_output()
        raise ValueError(f"Task or group {task_name} not found") 