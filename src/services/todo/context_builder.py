"""
Context builder for extracting minimal AI context from checkpoints.

This module implements smart context extraction strategies that build
minimal, relevant context for the planner AI based on interaction type.

SOLID Principles:
- Single Responsibility: Each builder handles one interaction type
- Open/Closed: Extensible for new interaction types
- Liskov Substitution: All builders implement same interface
- Interface Segregation: Separate interfaces for different contexts
- Dependency Inversion: Depends on abstractions
"""

import json
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any


class ContextBuilder(ABC):
    """Abstract base class for context builders."""
    
    @abstractmethod
    def build_context(
        self,
        checkpoint: Dict[str, Any],
        user_message: str,
        plan: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Build context for AI from checkpoint.
        
        Args:
            checkpoint: Latest checkpoint
            user_message: User's input message
            plan: Full plan (optional, for detailed task info)
            
        Returns:
            Context dictionary to send to AI
        """
        pass


class InitialPlanContextBuilder(ContextBuilder):
    """Builds context for initial plan creation."""
    
    def build_context(
        self,
        checkpoint: Dict[str, Any],
        user_message: str,
        plan: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Build minimal context for initial plan creation.
        
        Args:
            checkpoint: Latest checkpoint (may be None for first plan)
            user_message: User's goal
            plan: Full plan (not used for initial plan)
            
        Returns:
            Context with just the user goal
        """
        return {
            "interaction_type": "initial_plan",
            "user_goal": user_message,
            "instruction": "Create a structured plan with tasks to achieve this goal"
        }


class HumanLoopContextBuilder(ContextBuilder):
    """Builds context for human-loop interactions (user answering questions)."""
    
    def build_context(
        self,
        checkpoint: Dict[str, Any],
        user_message: str,
        plan: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Build context for user answering task-related questions.
        
        Args:
            checkpoint: Latest checkpoint
            user_message: User's answer (format: task_id:task_X, answer:...)
            plan: Full plan for task details
            
        Returns:
            Context with waiting task and user answer
        """
        compressed_state = checkpoint.get("compressed_state", {})
        waiting_tasks = compressed_state.get("waiting", [])
        
        task_contexts = []
        if plan and waiting_tasks:
            tasks = plan.get("tasks", {})
            for task_id in waiting_tasks:
                task = tasks.get(task_id, {})
                if task:
                    task_contexts.append({
                        "id": task_id,
                        "title": task.get("title", ""),
                        "question": task.get("human_query", ""),
                        "status": "waiting_for_user"
                    })
        
        completed_count = len(compressed_state.get("completed", []))
        total_tasks = compressed_state.get("total_tasks", 0)
        
        return {
            "interaction_type": "human_loop",
            "plan_summary": f"Progress: {completed_count}/{total_tasks} tasks completed",
            "waiting_tasks": task_contexts,
            "user_response": user_message,
            "instruction": "Update only the related tasks with user's answer. Do not change the goal or other tasks."
        }


class UpdateContextBuilder(ContextBuilder):
    """Builds context for plan update requests."""
    
    def build_context(
        self,
        checkpoint: Dict[str, Any],
        user_message: str,
        plan: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Build context for plan modification requests.
        
        Args:
            checkpoint: Latest checkpoint
            user_message: User's update request
            plan: Full plan for active task details
            
        Returns:
            Context with plan summary and active tasks
        """
        compressed_state = checkpoint.get("compressed_state", {})
        
        completed_count = len(compressed_state.get("completed", []))
        total_tasks = compressed_state.get("total_tasks", 0)
        
        completed_details = compressed_state.get("completed_details", [])
        completed_summary = [
            f"{detail['id']}: {detail['title']}"
            for detail in completed_details[-3:]
        ]
        
        active_tasks = []
        if plan:
            tasks = plan.get("tasks", {})
            for task_id in compressed_state.get("pending", []) + compressed_state.get("waiting", []):
                task = tasks.get(task_id, {})
                if task:
                    active_tasks.append({
                        "id": task_id,
                        "title": task.get("title", ""),
                        "status": task.get("status", "pending"),
                        "dependencies": task.get("dependencies", [])
                    })
        
        recent_changes = checkpoint.get("changes", [])[-3:]
        
        return {
            "interaction_type": "update",
            "plan_summary": f"Goal: {compressed_state.get('goal', '')}. Progress: {completed_count}/{total_tasks} tasks",
            "completed_summary": completed_summary,
            "active_tasks": active_tasks,
            "recent_changes": recent_changes,
            "user_request": user_message,
            "instruction": "Update the plan based on user's request. Preserve completed tasks and their results."
        }


class ExecutionUpdateContextBuilder(ContextBuilder):
    """Builds context for execution status updates."""
    
    def build_context(
        self,
        checkpoint: Dict[str, Any],
        user_message: str,
        plan: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Build context for execution progress updates.
        
        Args:
            checkpoint: Latest checkpoint
            user_message: Execution update message
            plan: Full plan for next task details
            
        Returns:
            Context with execution progress
        """
        compressed_state = checkpoint.get("compressed_state", {})
        executor_context = checkpoint.get("executor_context", {})
        
        completed_count = len(compressed_state.get("completed", []))
        total_tasks = compressed_state.get("total_tasks", 0)
        
        last_changes = checkpoint.get("changes", [])[-2:]
        
        next_task_id = executor_context.get("next_in_queue")
        next_task_info = None
        if next_task_id and plan:
            task = plan.get("tasks", {}).get(next_task_id, {})
            if task:
                next_task_info = {
                    "id": next_task_id,
                    "title": task.get("title", ""),
                    "status": task.get("status", "pending")
                }
        
        return {
            "interaction_type": "execution_update",
            "plan_summary": f"Progress: {completed_count}/{total_tasks} tasks completed",
            "last_completed": last_changes,
            "next_task": next_task_info,
            "runnable_tasks_count": len(executor_context.get("runnable_tasks", [])),
            "blocked_tasks_count": len(executor_context.get("blocked_tasks", []))
        }


class ContextBuilderFactory:
    """Factory for creating appropriate context builders."""
    
    @staticmethod
    def get_builder(interaction_type: str) -> ContextBuilder:
        """
        Get appropriate context builder for interaction type.
        
        Args:
            interaction_type: Type of interaction
            
        Returns:
            Appropriate context builder instance
        """
        builders = {
            "initial_plan": InitialPlanContextBuilder(),
            "human_loop": HumanLoopContextBuilder(),
            "update": UpdateContextBuilder(),
            "execution_update": ExecutionUpdateContextBuilder()
        }
        
        return builders.get(interaction_type, UpdateContextBuilder())
    
    @staticmethod
    def detect_interaction_type(
        user_message: str,
        checkpoint: Optional[Dict[str, Any]]
    ) -> str:
        """
        Detect interaction type from user message and checkpoint.
        
        Args:
            user_message: User's input message
            checkpoint: Latest checkpoint (None if first interaction)
            
        Returns:
            Interaction type string
        """
        if checkpoint is None:
            return "initial_plan"
        
        if "task_id:" in user_message.lower():
            return "human_loop"
        
        compressed_state = checkpoint.get("compressed_state", {})
        if compressed_state.get("waiting"):
            return "human_loop"
        
        if user_message is None or user_message == "":
            return "execution_update"
        
        return "update"


class SmartContextExtractor:
    """
    Main extractor that coordinates context building.
    
    Provides high-level interface for extracting minimal context from checkpoints.
    """
    
    def __init__(self):
        """Initialize smart context extractor."""
        self._factory = ContextBuilderFactory()
    
    def extract_context(
        self,
        checkpoint: Optional[Dict[str, Any]],
        user_message: str,
        plan: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Extract minimal context for AI from checkpoint.
        
        Args:
            checkpoint: Latest checkpoint (None for initial plan)
            user_message: User's input message
            plan: Full plan (optional, for detailed task info)
            
        Returns:
            Minimal context dictionary optimized for AI
        """
        interaction_type = self._factory.detect_interaction_type(user_message, checkpoint)
        
        builder = self._factory.get_builder(interaction_type)
        
        if checkpoint is None:
            checkpoint = {"compressed_state": {}, "changes": []}
        
        context = builder.build_context(checkpoint, user_message, plan)
        
        return context
    
    def get_token_estimate(self, context: Dict[str, Any]) -> int:
        """
        Estimate token count for context.
        
        Args:
            context: Context dictionary
            
        Returns:
            Estimated token count
        """
        context_str = json.dumps(context)
        return len(context_str) // 4
