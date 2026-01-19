# AI Middleware - Completion API Flow Documentation

## Overview
This document provides a comprehensive flow of the Completion API from request initiation to service execution. The flow demonstrates how a chat completion request is processed through various layers of middleware, configuration, and service handlers.

## API Entry Point

### Route Definition
- **Endpoint**: `/api/v2/model/chat/completion`
- **Method**: POST
- **File**: `index.py` (Line 167)
- **Router**: `v2_router` with prefix `/api/v2/model`

### Route Handler
- **File**: `src/routes/v2/modelRouter.py`
- **Function**: `chat_completion`
- **Dependencies**: 
  - `auth_and_rate_limit` (JWT middleware + rate limiting)
  - `add_configuration_data_to_body` (configuration middleware)

## Flow Breakdown

### 1. Request Processing & Middleware

#### Authentication & Rate Limiting
- **JWT Middleware**: Validates authentication tokens
- **Rate Limiting**: 
  - 100 points per `bridge_id`
  - 20 points per `thread_id`

#### Configuration Middleware
- **File**: `src/middlewares/getDataUsingBridgeId.py`
- **Function**: `add_configuration_data_to_body`
- **Purpose**: Enriches request body with configuration data

**Key Operations:**
- Extracts `bridge_id`, `org_id`, `version_id` from request
- Calls `getConfiguration()` to fetch complete configuration
- Validates model and service existence
- Validates organization permissions for custom models

**Required Fields:**
- `user` message (mandatory unless images provided)
- Valid `service` and `model` combination
- Valid `bridge_id` or `agent_id`

### 2. Configuration Retrieval

#### Main Configuration Function
- **File**: `src/services/utils/getConfiguration.py`
- **Function**: `getConfiguration`

**Input Parameters:**
- `configuration`: Base configuration to merge
- `service`: AI service name
- `bridge_id`: Bridge identifier
- `apikey`: API key for service
- `template_id`: Optional template ID
- `variables`: Variables for prompt replacement
- `org_id`: Organization ID
- `variables_path`: Path for variables
- `version_id`: Version ID for bridge
- `extra_tools`: Additional tools to include. Each tool supports optional `headers` and `toolAndVariablePath` (or `tool_and_variable_path`) to map tool input fields to values pulled from the request `variables`, preventing those fields from being surfaced to the model.
- `built_in_tools`: Built-in tools to include

#### Bridge Data Retrieval
- **File**: `src/services/utils/getConfiguration_utils.py`
- **Function**: `get_bridge_data`
- **Database Service**: `src/db_services/ConfigurationServices.py`
- **Function**: `get_bridges_with_tools_and_apikeys`

**Database Operations:**
- Aggregation pipeline joins multiple collections:
  - `bridges` (main configuration)
  - `apicalls` (tools/functions)
  - `apikeycredentials` (API keys)
  - `rag_parent_datas` (RAG documents)
- Redis caching for performance optimization
- Converts ObjectIds to strings for JSON serialization

**Retrieved Data:**
- Bridge configuration and settings
- Associated tools and API calls
- API keys for different services
- RAG (Retrieval Augmented Generation) data
- Pre-tools configuration
- Memory settings and context

#### Configuration Assembly
The `getConfiguration` function assembles:

**Core Configuration:**
- `prompt`: System prompt with tone and response style
- `model`: AI model to use
- `tools`: Available function tools
- `tool_choice`: Tool selection strategy
- `temperature`, `max_tokens`, etc.: Model parameters

**Metadata:**
- `service`: AI service provider
- `apikey`: Service API key
- `bridge_id`: Bridge identifier
- `org_id`: Organization ID
- `variables`: Prompt variables
- `rag_data`: Document data for RAG
- `gpt_memory`: Memory settings
- `tool_call_count`: Maximum tool calls allowed

**Output Structure:**
```json
{
  "success": true,
  "configuration": { /* merged configuration */ },
  "service": "openai",
  "apikey": "sk-...",
  "pre_tools": { "name": "tool_name", "args": {} },
  "variables": { /* processed variables */ },
  "rag_data": [ /* document data */ ],
  "tools": [ /* available tools */ ],
  "tool_id_and_name_mapping": { /* tool mappings */ },
  "gpt_memory": true,
  "version_id": "version_123",
  "bridge_id": "bridge_456"
}
```

### 3. Request Routing Decision

#### Response Format Check
- **Queue Processing**: If `response_format.type != 'default'`
  - Publishes message to queue for async processing
  - Returns immediate acknowledgment
- **Synchronous Processing**: For default response format
  - Continues to chat function

#### Type-Based Routing
- **Embedding Type**: Routes to `embedding()` function
- **Chat Type**: Routes to `chat()` function (main flow)

### 4. Chat Function Processing

#### File: `src/services/commonServices/common.py`
#### Function: `chat(request_body)`

**Step-by-Step Processing:**

1. **Request Parsing** (`parse_request_body`)
   - Extracts all required fields from request body
   - Initializes default values
   - Creates structured data object

2. **Template Enhancement**
   - Adds default template with current time reference
   - Adds user message to variables as `_user_message`

3. **Timer Initialization**
   - Creates Timer object for performance tracking
   - Starts timing for overall API execution

4. **Model Configuration Loading**
   - Loads model configuration from `model_config_document`
   - Extracts custom configuration based on user input
   - Handles fine-tuned model selection

5. **Pre-Tools Execution**
   - Executes pre-configured tools if specified
   - Makes HTTP calls to external functions
   - Stores results in variables

6. **Thread Management**
   - Creates or retrieves conversation thread
   - Manages sub-thread relationships
   - Loads conversation history

7. **Prompt Preparation**
   - Replaces variables in prompt template
   - Applies system templates if specified
   - Handles memory context for GPT memory
   - Identifies missing required variables

8. **Custom Settings Configuration**
   - Applies service-specific configurations
   - Handles response type conversions
   - Manages JSON schema formatting

9. **Service Parameter Building**
   - Assembles all parameters for service execution
   - Includes token calculator for cost tracking
   - Prepares execution context

### 5. Service Handler Creation and Execution

#### Service Handler Factory
- **File**: `src/services/utils/helper.py`
- **Function**: `Helper.create_service_handler`

**Service Mapping:**
- `openai` → `UnifiedOpenAICase`
- `gemini` → `GeminiHandler`
- `anthropic` → `Anthropic`
- `groq` → `Groq`
- `openai_response` → `OpenaiResponse`
- `open_router` → `OpenRouter`
- `mistral` → `Mistral`

#### Service Execution
- **Method**: `class_obj.execute()`
- **Purpose**: Executes the AI service call with prepared parameters
- **Returns**: Service response with usage metrics and content

## Key Data Structures

### Parsed Request Data
```json
{
  "bridge_id": "string",
  "configuration": { /* AI model config */ },
  "thread_id": "string",
  "sub_thread_id": "string", 
  "org_id": "string",
  "user": "string",
  "service": "string",
  "model": "string",
  "variables": { /* prompt variables */ },
  "tools": [ /* available tools */ ],
  "is_playground": boolean,
  "response_format": { /* response formatting */ },
  "files": [ /* uploaded files */ ],
  "images": [ /* image inputs */ ]
}
```

### Service Parameters
```json
{
  "customConfig": { /* model-specific config */ },
  "configuration": { /* full configuration */ },
  "apikey": "string",
  "user": "string",
  "tools": [ /* function tools */ ],
  "org_id": "string",
  "bridge_id": "string",
  "thread_id": "string",
  "model": "string",
  "service": "string",
  "token_calculator": { /* cost tracking */ },
  "variables": { /* prompt variables */ },
  "memory": "string",
  "rag_data": [ /* document data */ ]
}
```

## Error Handling

### Validation Errors
- Missing required fields (user message)
- Invalid model/service combinations
- Organization permission violations
- Invalid bridge_id or configuration

### Processing Errors
- Database connection failures
- External API call failures
- Template processing errors
- Variable replacement failures

### Response Format
```json
{
  "success": false,
  "error": "Error description"
}
```

## Performance Optimizations

### Caching Strategy
- **Redis Cache**: Bridge configurations cached by `bridge_id` or `version_id`
- **File Cache**: Uploaded files cached by thread context
- **Memory Cache**: GPT memory context cached by thread ID

### Async Processing
- **Queue System**: Non-default response formats processed asynchronously
- **Background Tasks**: Metrics and logging handled in background
- **Thread Pool**: Executor for CPU-intensive operations

## Security Considerations

### Authentication
- JWT token validation required
- Organization-level access control
- API key validation per service

### Rate Limiting
- Per-bridge rate limiting (100 points)
- Per-thread rate limiting (20 points)
- Configurable rate limit windows

### Data Isolation
- Organization-level data segregation
- Bridge-level permission validation
- Secure API key storage and retrieval

## 6. Service Execution (OpenAI Example)

### Service Handler Execution
- **File**: `src/services/commonServices/openAI/openaiCall.py`
- **Class**: `UnifiedOpenAICase`
- **Method**: `execute()`

#### Conversation Creation
- **Function**: `ConversationService.createOpenAiConversation`
- **File**: `src/services/commonServices/createConversations.py`
- **Purpose**: Converts conversation history to OpenAI format

**Conversation Processing:**
- Adds memory context if GPT memory is enabled
- Processes conversation history with role-based formatting
- Handles image URLs and file attachments
- Creates proper message structure for different AI services

**Service-Specific Conversation Formats:**
- `createOpenAiConversation`: Standard OpenAI chat format
- `createOpenAiResponseConversation`: OpenAI Response API format
- `createAnthropicConversation`: Anthropic Claude format
- `createGroqConversation`: Groq API format
- `createGeminiConversation`: Google Gemini format
- `create_mistral_ai_conversation`: Mistral AI format

#### Model API Call
- **Function**: `self.chats()`
- **File**: `src/services/commonServices/baseService/baseService.py`
- **Purpose**: Routes to appropriate service model runner

**Service Routing:**
- `openai` → `runModel` (OpenAI API)
- `openai_response` → `openai_response_model` (OpenAI Response API)
- `anthropic` → `anthropic_runmodel`
- `groq` → `groq_runmodel`
- `gemini` → `gemini_modelrun`
- `mistral` → `mistral_model_run`
- `open_router` → `openrouter_modelrun`

#### OpenAI Model Execution
- **File**: `src/services/commonServices/openAI/runModel.py`
- **Function**: `runModel`

**Key Features:**
- Async OpenAI client initialization
- Retry mechanism with alternative model fallback
- Performance timing and logging
- Error handling with status codes
- Support for both standard and response APIs

**Retry Logic:**
- Primary model: `o3` → Fallback: `gpt-4o-2024-08-06`
- Primary model: `gpt-4o` → Fallback: `o3`
- Default fallback: `gpt-4o`

## 7. Tool Call Detection and Execution

### Tool Call Detection
- **Check**: `len(modelResponse.get('choices', [])[0].get('message', {}).get("tool_calls", [])) > 0`
- **Purpose**: Determines if AI model wants to execute functions

### Function Call Processing
- **Function**: `self.function_call()`
- **File**: `src/services/commonServices/baseService/baseService.py`
- **Recursive**: Supports multiple rounds of tool calling

#### Tool Call Flow:

1. **Tool Validation**: Checks if response contains valid tool calls
2. **Tool Execution**: Runs requested tools concurrently
3. **Configuration Update**: Adds tool results to conversation
4. **Model Re-call**: Sends updated conversation back to AI
5. **Recursion**: Repeats until no more tool calls or limit reached

#### Tool Execution Process
- **Function**: `self.run_tool()`
- **Steps**:
  1. **Code Mapping**: `make_code_mapping_by_service()` - Extracts tool calls by service
  2. **Variable Replacement**: `replace_variables_in_args()` - Processes tool arguments
  3. **Tool Processing**: `process_data_and_run_tools()` - Executes tools concurrently

#### Service-Specific Tool Call Extraction
- **OpenAI/Groq/Mistral**: `tool_calls` array with `function` objects
- **OpenAI Response**: `output` array with `function_call` type
- **Anthropic**: `content` array with `tool_use` type
- **Gemini**: Similar to OpenAI format

#### Tool Types and Execution
- **Regular Tools**: HTTP calls to external APIs
- **RAG Tools**: Vector database queries
- **Agent Tools**: Calls to other AI agents
- **Built-in Tools**: Internal system functions

**Concurrent Execution:**
- Uses `asyncio.gather()` for parallel tool execution
- Handles exceptions gracefully
- Returns formatted responses for each tool call

#### Tool Call Limits
- **Maximum Rounds**: Configurable per bridge (`tool_call_count`)
- **Default**: 3 rounds of tool calling
- **Prevention**: Avoids infinite loops

## 8. Response Processing and Formatting

### Response Formatting
- **Function**: `Response_formatter()`
- **File**: `src/services/utils/ai_middleware_format.py`
- **Purpose**: Standardizes responses across all AI services

#### Unified Response Structure
```json
{
  "data": {
    "id": "response_id",
    "content": "AI response text",
    "model": "model_name",
    "role": "assistant",
    "finish_reason": "stop",
    "tools_data": { /* tool execution results */ },
    "images": [ /* image URLs */ ],
    "annotations": [ /* response annotations */ ],
    "fallback": false,
    "firstAttemptError": ""
  },
  "usage": {
    "input_tokens": 100,
    "output_tokens": 50,
    "total_tokens": 150,
    "cached_tokens": 20
  }
}
```

#### Service-Specific Formatting
- **OpenAI**: Standard chat completion format
- **OpenAI Response**: Special handling for reasoning and function calls
- **Anthropic**: Content array processing
- **Gemini**: Google-specific response structure
- **Groq**: Similar to OpenAI with service-specific fields

### History Preparation
- **Function**: `prepare_history_params()`
- **Purpose**: Creates data structure for conversation storage

**History Parameters:**
- Thread and message identification
- User input and AI response
- Tool execution data
- Model configuration
- Usage metrics
- Error information
- File attachments

## 9. Final Processing and Response

### Post-Execution Processing
- **File**: `src/services/commonServices/common.py`
- **Function**: `chat()` (continuation)

#### Success Flow:
1. **Error Checking**: Validates service execution success
2. **Retry Alerts**: Handles fallback model notifications
3. **Chatbot Processing**: Special handling for chatbot responses
4. **Usage Calculation**: Token and cost calculations
5. **Response Formatting**: Final response structure
6. **Background Tasks**: Async database operations

#### Response Delivery
- **Playground Mode**: Direct JSON response
- **Production Mode**: 
  - WebSocket/webhook delivery for configured formats
  - Database storage
  - Usage tracking
  - Error notifications

### Background Data Processing
- **Function**: `process_background_tasks()`
- **File**: `src/services/utils/common_utils.py`

**Background Operations:**
1. **Metrics Creation**: `create()` from metrics_service
2. **Sub-queue Publishing**: Message queue for downstream processing
3. **Conversation Storage**: Thread and message persistence

## 10. Database Operations and Metrics

### Metrics Collection
- **File**: `src/db_services/metrics_service.py`
- **Function**: `create()`

#### Data Storage Flow:
1. **Conversation History**: Saves to conversation database
2. **Raw Data Insertion**: PostgreSQL metrics table
3. **TimescaleDB Metrics**: Time-series data for analytics
4. **Token Caching**: Redis cache for usage tracking

#### Stored Metrics:
- **Usage Data**: Input/output tokens, costs
- **Performance Data**: Latency, execution times
- **Error Data**: Failure reasons, retry information
- **Configuration Data**: Model settings, tool usage
- **Organizational Data**: Bridge, version, user tracking

### Error Handling and Alerts
- **Webhook Notifications**: Real-time error alerts
- **Usage Limit Tracking**: Token consumption monitoring
- **Performance Monitoring**: Latency and failure tracking
- **Retry Mechanism Alerts**: Fallback model usage notifications

## Complete Flow Summary

### Request Journey:
1. **API Entry** → Authentication & Rate Limiting
2. **Middleware** → Configuration Retrieval & Validation
3. **Chat Function** → Request Processing & Setup
4. **Service Handler** → AI Model Execution
5. **Tool Processing** → Function Call Execution (if needed)
6. **Response Formatting** → Standardized Output
7. **Background Tasks** → Database Storage & Metrics
8. **Response Delivery** → Client Response

### Key Performance Features:
- **Concurrent Tool Execution**: Parallel function calling
- **Redis Caching**: Configuration and usage caching
- **Retry Mechanisms**: Automatic fallback handling
- **Background Processing**: Non-blocking database operations
- **Connection Pooling**: Efficient database connections

### Error Recovery:
- **Model Fallbacks**: Alternative model execution
- **Tool Error Handling**: Graceful function failure management
- **Database Resilience**: Error logging and recovery
- **User Notifications**: Real-time error feedback

This complete flow ensures robust, scalable, and reliable AI service execution with comprehensive monitoring and error handling capabilities.
