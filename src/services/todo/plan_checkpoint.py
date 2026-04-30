"""
Checkpoint management for planning mode.

This module implements a checkpoint-based state management system that compresses
plan state and tracks execution history for efficient AI context building.

SOLID Principles:
- Single Responsibility: Each class handles one aspect of checkpoint management
- Open/Closed: Extensible for new compression strategies
- Liskov Substitution: Compression strategies are interchangeable
- Interface Segregation: Separate interfaces for different checkpoint operations
- Dependency Inversion: Depends on abstractions, not concrete implementations
"""

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any


class CompressionStrategy(ABC):
    """Abstract base class for plan compression strategies."""
    
    @abstractmethod
    def compress(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Compress plan state into a minimal representation."""
        pass


class StandardCompressionStrategy(CompressionStrategy):
    """Standard compression strategy for plan state."""
    
    def compress(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compress plan by categorizing tasks and summarizing completed work.
        
        Args:
            plan: Full plan dictionary
            
        Returns:
            Compressed state with categorized tasks
        """
        tasks = plan.get("tasks", {})
        
        completed = []
        pending = []
        waiting = []
        failed = []
        
        completed_details = []
        
        for task_id, task in tasks.items():
            status = task.get("status", "pending")
            
            if status == "completed":
                completed.append(task_id)
                completed_details.append({
                    "id": task_id,
                    "title": task.get("title", ""),
                    "result_summary": self._summarize_result(task.get("result"))
                })
            elif status == "pending" or status == "in_progress":
                pending.append(task_id)
            elif status == "waiting_for_user":
                waiting.append(task_id)
            elif status == "failed":
                failed.append(task_id)
        
        return {
            "goal": plan.get("goal", ""),
            "total_tasks": len(tasks),
            "completed": completed,
            "pending": pending,
            "waiting": waiting,
            "failed": failed,
            "completed_details": completed_details
        }
    
    def _summarize_result(self, result: Any) -> str:
        """Summarize task result to first 100 characters."""
        if result is None:
            return ""
        
        result_str = str(result)
        if len(result_str) > 100:
            return result_str[:97] + "..."
        return result_str


class CheckpointFactory:
    """Factory for creating checkpoints with different strategies."""
    
    def __init__(self, compression_strategy: CompressionStrategy):
        """
        Initialize factory with compression strategy.
        
        Args:
            compression_strategy: Strategy to use for compressing plan state
        """
        self._compression_strategy = compression_strategy
    
    def create_checkpoint(
        self,
        plan: Dict[str, Any],
        interaction_type: str,
        user_message: Optional[str] = None,
        changes: Optional[List[str]] = None,
        tool_calls: Optional[List[Dict]] = None,
        questions_asked: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Create a new checkpoint from plan state.
        
        Args:
            plan: Full plan dictionary
            interaction_type: Type of interaction (initial_plan, update, human_loop, etc.)
            user_message: User's message for this interaction
            changes: List of changes made in this checkpoint
            tool_calls: Tool calls made during this interaction
            questions_asked: Questions asked by AI during this interaction
            
        Returns:
            Checkpoint dictionary
        """
        compressed_state = self._compression_strategy.compress(plan)
        
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "interaction_type": interaction_type,
            "compressed_state": compressed_state,
            "user_message": user_message,
            "changes": changes or [],
            "ai_actions": {
                "tool_calls": tool_calls or [],
                "questions_asked": questions_asked or []
            }
        }


class ExecutorContextBuilder:
    """Builds executor-specific context from plan state."""
    
    def build_executor_context(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build executor context with runnable tasks and dependency graph.
        
        Args:
            plan: Full plan dictionary
            
        Returns:
            Executor context with runnable and blocked tasks
        """
        tasks = plan.get("tasks", {})
        runnable = []
        blocked = []
        dependency_graph = {}
        
        for task_id, task in tasks.items():
            if task.get("status") != "pending":
                continue
            
            deps = task.get("dependencies", [])
            all_met = all(
                tasks.get(dep, {}).get("status") == "completed"
                for dep in deps
            )
            
            dependency_graph[task_id] = {
                "depends_on": deps,
                "all_met": all_met
            }
            
            if all_met:
                runnable.append(task_id)
            else:
                blocked.append(task_id)
        
        return {
            "runnable_tasks": runnable,
            "blocked_tasks": blocked,
            "next_in_queue": runnable[0] if runnable else None,
            "dependency_graph": dependency_graph
        }


class DeltaComputer:
    """Computes deltas between checkpoint versions."""
    
    def compute_delta(
        self,
        old_checkpoint: Optional[Dict[str, Any]],
        new_plan: Dict[str, Any]
    ) -> List[str]:
        """
        Compute changes between old checkpoint and new plan state.
        
        Args:
            old_checkpoint: Previous checkpoint (None if first checkpoint)
            new_plan: Current plan state
            
        Returns:
            List of change descriptions
        """
        if old_checkpoint is None:
            return [f"Initial plan created with {len(new_plan.get('tasks', {}))} tasks"]
        
        changes = []
        old_state = old_checkpoint.get("compressed_state", {})
        
        compression = StandardCompressionStrategy()
        new_state = compression.compress(new_plan)
        
        old_completed = set(old_state.get("completed", []))
        new_completed = set(new_state.get("completed", []))
        newly_completed = new_completed - old_completed
        
        for task_id in newly_completed:
            task = new_plan.get("tasks", {}).get(task_id, {})
            changes.append(f"{task_id} completed: {task.get('title', 'Unknown')}")
        
        old_waiting = set(old_state.get("waiting", []))
        new_waiting = set(new_state.get("waiting", []))
        newly_waiting = new_waiting - old_waiting
        
        for task_id in newly_waiting:
            task = new_plan.get("tasks", {}).get(task_id, {})
            changes.append(f"{task_id} waiting for user: {task.get('human_query', 'Question pending')}")
        
        old_failed = set(old_state.get("failed", []))
        new_failed = set(new_state.get("failed", []))
        newly_failed = new_failed - old_failed
        
        for task_id in newly_failed:
            task = new_plan.get("tasks", {}).get(task_id, {})
            changes.append(f"{task_id} failed: {task.get('error', 'Unknown error')}")
        
        old_total = old_state.get("total_tasks", 0)
        new_total = new_state.get("total_tasks", 0)
        if new_total > old_total:
            changes.append(f"{new_total - old_total} new task(s) added to plan")
        
        return changes if changes else ["Plan state unchanged"]


class CheckpointManager:
    """
    Main manager for checkpoint operations.
    
    Coordinates checkpoint creation, compression, and delta computation.
    """
    
    def __init__(
        self,
        compression_strategy: Optional[CompressionStrategy] = None
    ):
        """
        Initialize checkpoint manager.
        
        Args:
            compression_strategy: Strategy for compressing plan state
        """
        self._compression_strategy = compression_strategy or StandardCompressionStrategy()
        self._factory = CheckpointFactory(self._compression_strategy)
        self._executor_builder = ExecutorContextBuilder()
        self._delta_computer = DeltaComputer()
    
    def create_checkpoint(
        self,
        plan: Dict[str, Any],
        interaction_type: str,
        user_message: Optional[str] = None,
        old_checkpoint: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict]] = None,
        questions_asked: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Create a complete checkpoint with all metadata.
        
        Args:
            plan: Full plan dictionary
            interaction_type: Type of interaction
            user_message: User's message
            old_checkpoint: Previous checkpoint for delta computation
            tool_calls: Tool calls made
            questions_asked: Questions asked by AI
            
        Returns:
            Complete checkpoint with compressed state, executor context, and changes
        """
        changes = self._delta_computer.compute_delta(old_checkpoint, plan)
        
        checkpoint = self._factory.create_checkpoint(
            plan=plan,
            interaction_type=interaction_type,
            user_message=user_message,
            changes=changes,
            tool_calls=tool_calls,
            questions_asked=questions_asked
        )
        
        checkpoint["executor_context"] = self._executor_builder.build_executor_context(plan)
        
        return checkpoint
    
    def compress_plan_state(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compress plan state using configured strategy.
        
        Args:
            plan: Full plan dictionary
            
        Returns:
            Compressed state
        """
        return self._compression_strategy.compress(plan)
