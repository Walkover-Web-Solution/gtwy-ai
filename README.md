# GTWY AI (Python Backend - FastAPI)

**One API for All AI Services** - Build, orchestrate, and deploy intelligent AI agents with ease.

GTWY AI is an open-source unified AI middleware that simplifies AI integration across multiple providers. Create powerful AI agents, orchestrate multi-agent workflows, and connect to 5000+ apps - all through a single, consistent API.

## ✨ What You Can Build

- 🤖 **Create AI Agents**: Design custom AI agents with specific capabilities and personas
- 🔄 **Orchestrate Multi-Agent Systems**: Build complex workflows where agents collaborate and transfer tasks
- 💬 **Deploy as API or Chatbot**: Use your agents through REST APIs or integrate as conversational interfaces
- 📚 **RAG as a Service**: Implement retrieval-augmented generation with built-in document processing and vector search
- 🔌 **5000+ App Connectors**: Integrate with thousands of external services and tools through agent connections

## 📚 Documentation
For detailed architecture and flow documentation, see:
- [Chat Completion Flow](docs/CHAT_COMPLETION_FLOW.md) - Comprehensive API flow from request to response

## 🚀 Features

### Multi-Provider AI Support
- **OpenAI**: GPT models, embeddings, image generation, batch processing
- **Anthropic**: Claude models with vision support, batch processing
- **Google Gemini**: Text, image, and video processing capabilities
- **Groq**: High-performance inference with batch support
- **Mistral AI**: Advanced language models with batch processing
- **OpenRouter**: Access to multiple AI providers through a unified interface
- **Grok**: xAI's language models
- **Custom AI/ML**: Support for custom model endpoints

### Advanced Features
- **Multi-Agent Orchestration**: Configure and manage multiple AI agents with automatic tool generation and transfer capabilities
- **Retrieval-Augmented Generation (RAG)**: Document chunking, vector search, and context-aware responses
- **Function/Tool Calling**: Built-in and custom tools with parallel execution support
- **Batch Processing**: Async batch job creation and monitoring across multiple providers
- **Memory Management**: Conversation history with configurable context windows
- **Guardrails**: Content filtering and validation
- **Web Search Integration**: Built-in web search tools with domain filtering
- **Image Processing**: Multi-modal support for image analysis and generation
- **Pre-built Prompts**: Reusable prompt templates and configurations

## 🛠️ Installation
```bash
# Clone the repo
git clone https://github.com/Walkover-Web-Solution/AI-middleware-python.git
cd AI-middleware-python

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate   # Linux/Mac
venv\Scripts\activate      # Windows

# Install dependencies
pip install -r req.txt

# Run the FastAPI server
uvicorn app.main:app --reload
