# GTWY AI (Python Backend - FastAPI)

GTWY AI is an open-source project aimed at building intelligent, scalable, and community-driven AI services.
This repository contains the **Python backend built with FastAPI**, which powers the core APIs.

## 🚀 Features
- Fast and asynchronous API built with FastAPI
- Easy-to-extend modular structure
- Environment-based configuration
- Open for community-driven extensions
- RAG implementation
- Chatbot implementation

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
