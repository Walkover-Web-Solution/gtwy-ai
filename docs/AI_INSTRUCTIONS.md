# AI Instructions - Code Contribution Guide

This document provides comprehensive guidelines for writing new code in the GTWY AI middleware repository. Follow these conventions to maintain consistency and code quality.

## Table of Contents
1. [AI Coding Mental Model](#ai-coding-mental-model)
2. [Project Structure](#project-structure)
3. [File Naming Conventions](#file-naming-conventions)
4. [Code Organization](#code-organization)
5. [Coding Standards](#coding-standards)
6. [Error Handling](#error-handling)
7. [Best Practices](#best-practices)

---

## AI Coding Mental Model

**Act as a system designer.**

### Core Principles

**Never guess.**
- If intent, constraints, or compatibility with the system are unclear, ask questions before acting.
- Understand requirements fully before implementing.
- Clarify ambiguous specifications.

**The system comes first.**
- Architecture, design decisions, and existing guidelines define what is allowed.
- Resolve conceptual conflicts before writing code.
- Follow established patterns and conventions.
- Respect the existing codebase structure.

**Understanding is shared memory.**
- When understanding changes, update code and its documentation together so the system stays coherent.
- Keep documentation synchronized with implementation.
- Update related files when making changes.
- Maintain consistency across the codebase.

---

## Project Structure

```
AI-middleware-python/
├── index.py                    # Main application entry point
├── config.py                   # Configuration management
├── globals.py                  # Global variables and logger
├── exceptions/                 # Custom exception classes
├── models/                     # Database models and connections
│   ├── mongo_connection.py
│   ├── postgres/
│   └── Timescale/
├── src/
│   ├── configs/               # Configuration constants and model configs
│   ├── controllers/           # Request handlers (business logic)
│   ├── db_services/           # Database service layer
│   ├── handler/               # Execution handlers
│   ├── middlewares/           # Request middleware (auth, rate limiting)
│   ├── routes/                # API route definitions
│   │   └── v2/               # Version 2 API routes
│   └── services/              # Core business services
│       ├── commonServices/    # AI provider services
│       │   ├── openAI/
│       │   ├── anthropic/
│       │   ├── Google/
│       │   ├── groq/
│       │   ├── Mistral/
│       │   ├── grok/
│       │   ├── openRouter/
│       │   ├── AiMl/
│       │   ├── baseService/
│       │   └── queueService/
│       ├── rag_services/      # RAG-specific services
│       ├── proxy/             # Proxy services
│       └── utils/             # Utility functions
└── docs/                      # Documentation
```

---

## File Naming Conventions

### General Rules
- Use **snake_case** for file names: `my_service.py`, `user_controller.py`
- Use descriptive names that clearly indicate the file's purpose
- Avoid abbreviations unless commonly understood (e.g., `rag`, `api`, `db`)

### Specific Patterns

#### Controllers
- **Location**: `src/controllers/`
- **Pattern**: `{feature}_controller.py`
- **Examples**: `rag_controller.py`, `conversationController.py`, `image_process_controller.py`

#### Routes
- **Location**: `src/routes/` or `src/routes/v2/`
- **Pattern**: `{feature}_routes.py` or `{feature}Router.py`
- **Examples**: `rag_routes.py`, `chatBot_routes.py`, `modelRouter.py`

#### Services
- **Location**: `src/services/` or `src/services/commonServices/{provider}/`
- **Pattern**: `{feature}_service.py` or `{provider}Call.py`
- **Examples**: 
  - `cache_service.py`, `testcase_service.py`
  - `anthropicCall.py`, `geminiCall.py`, `groqCall.py`

#### Database Services
- **Location**: `src/db_services/`
- **Pattern**: `{feature}Services.py` or `{feature}_service.py`
- **Examples**: `ConfigurationServices.py`, `conversationDbService.py`, `metrics_service.py`

#### Middlewares
- **Location**: `src/middlewares/`
- **Pattern**: `{feature}Middleware.py` or `{feature}Middlewares.py`
- **Examples**: `middleware.py`, `ratelimitMiddleware.py`, `agentsMiddlewares.py`

#### Utilities
- **Location**: `src/services/utils/`
- **Pattern**: `{feature}_utils.py` or `{feature}.py`
- **Examples**: `rag_utils.py`, `common_utils.py`, `logger.py`, `helper.py`

#### AI Provider Services
- **Location**: `src/services/commonServices/{provider}/`
- **Patterns**:
  - `{provider}Call.py` - Main API call handler
  - `{provider}ModelRun.py` or `{provider}_model_run.py` - Model execution logic
  - `{provider}_batch.py` - Batch processing
  - `{provider}_run_batch.py` - Batch execution
  - `{provider}_image_model.py` - Image processing (if applicable)
- **Examples**:
  - `anthropicCall.py`, `anthropicModelRun.py`
  - `gemini_batch.py`, `gemini_run_batch.py`

---

## Code Organization

### Module Structure Rules

1. **Import Order** (Always follow this sequence):
   - First: Standard library imports
   - Second: Third-party imports
   - Third: Local application imports

2. **File Structure Order**:
   - Constants at the top
   - Helper functions
   - Main classes
   - Route handlers or main functions

---

## Coding Standards

### 1. Import Organization
- Group imports: standard library → third-party → local
- Use absolute imports for local modules
- Avoid wildcard imports (`from module import *`)
- Remove unused imports

### 2. Async/Await
- Use `async`/`await` for all I/O operations
- Database queries must be async
- HTTP requests must be async
- File operations must be async
- Use `asyncio.create_task()` for concurrent operations

### 3. Type Hints
- Use type hints for function parameters and return types
- Use `Optional` for nullable values
- Use `Dict`, `List`, `Tuple` from `typing` module
- Use `Any` sparingly, prefer specific types

### 4. Error Handling
- Always use try-except blocks for external calls
- Log errors with appropriate log levels
- Raise HTTPException with proper status codes
- Include context in error messages
- Never ignore exceptions silently

### 5. Logging
- Use the global logger from `globals.py`
- Log levels: `logger.info()`, `logger.error()`, `logger.warning()`, `logger.debug()`
- Include context in all log messages (e.g., bridge_id, org_id)
- Never use print statements
- Log before raising exceptions

### 6. Configuration
- Use `Config` class from `config.py` for environment variables
- Never hardcode credentials or URLs
- Access config values: `Config.VARIABLE_NAME`
- All environment-specific values must come from Config

---

## Error Handling

### Custom Exceptions Rules
- **Location**: `exceptions/`
- Create specific exception classes for different error types
- Include status codes in custom exceptions
- Inherit from appropriate base exceptions

### Error Response Rules
- Always return consistent error responses
- Include helpful error messages for debugging
- Log errors before raising
- Use appropriate HTTP status codes:
  - 400: Bad Request (validation errors)
  - 401: Unauthorized
  - 403: Forbidden
  - 404: Not Found
  - 500: Internal Server Error

---

## Best Practices

### DO ✅
- Use async/await for all I/O operations
- Add proper error handling and logging
- Use type hints for all functions
- Follow naming conventions strictly
- Use the global logger from `globals.py`
- Cache frequently accessed data with appropriate TTL
- Validate input data before processing
- Use environment variables from Config
- Write descriptive commit messages
- Keep functions small and focused (single responsibility)
- Use meaningful variable and function names
- Document complex logic with comments
- Handle edge cases and null values
- Use context managers for resources
- Follow DRY principle (Don't Repeat Yourself)

### DON'T ❌
- Don't hardcode credentials, API keys, or URLs
- Don't use blocking I/O operations (synchronous database calls)
- Don't ignore exceptions silently
- Don't mix business logic with route handlers
- Don't commit sensitive data (.env files, credentials)
- Don't use print statements (use logger instead)
- Don't create circular imports
- Don't duplicate code (create utilities instead)
- Don't use wildcard imports
- Don't leave commented-out code
- Don't use generic variable names (e.g., `data`, `temp`, `x`)
- Don't skip error handling
- Don't forget to clean up resources

---

## Adding New Features

### Adding AI Provider Service
1. Create provider directory: `src/services/commonServices/{provider}/`
2. Create required files:
   - `{provider}Call.py` - Main handler
   - `{provider}ModelRun.py` - Model execution
   - `{provider}_batch.py` - Batch processing (if needed)
3. Inherit from `BaseService` class
4. Add provider to service mapping in `helper.py`

### Adding Database Service
1. Create service file in `src/db_services/`
2. Use pattern: `{feature}Services.py` or `{feature}_service.py`
3. Define collection/table at module level
4. Create async functions for CRUD operations
5. Include proper error handling and logging

### Adding Middleware
1. Create middleware file in `src/middlewares/`
2. Use pattern: `{feature}Middleware.py`
3. Create async function that accepts `Request` parameter
4. Validate and attach data to `request.state` if needed
5. Raise HTTPException for validation failures

---

## Code Review Checklist

Before submitting code, ensure:
- [ ] Follows naming conventions
- [ ] Has proper error handling with try-except blocks
- [ ] Includes logging with appropriate levels
- [ ] Uses async/await correctly for all I/O
- [ ] Has type hints for parameters and return types
- [ ] No hardcoded values (credentials, URLs, etc.)
- [ ] Follows project structure
- [ ] Code is documented with comments for complex logic
- [ ] No unused imports or variables
- [ ] Tested locally
- [ ] No print statements (uses logger)
- [ ] Proper import organization
- [ ] Error messages are descriptive
- [ ] Uses Config for environment variables

---

## Additional Resources

- [Chat Completion Flow Documentation](./CHAT_COMPLETION_FLOW.md)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [MongoDB Async Driver](https://motor.readthedocs.io/)
- [Python Async/Await](https://docs.python.org/3/library/asyncio.html)

---

## Questions or Issues?

If you have questions about these guidelines or encounter issues:
1. Check existing code for examples
2. Review the documentation in `docs/`
3. Ask the development team
4. Create an issue with the `question` label

---

**Last Updated**: January 2026
