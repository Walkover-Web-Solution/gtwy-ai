import io

import docx
import pandas as pd
import PyPDF2
from fastapi import UploadFile

from src.configs.constant import bridge_ids

from .ai_call_util import call_ai_middleware


async def extract_pdf_text(file: UploadFile) -> str:
    pdf_reader = PyPDF2.PdfReader(io.BytesIO(await file.read()))
    text = ""
    for page in pdf_reader.pages:
        text += page.extract_text()
    return text


# Function to extract text from DOCX file
async def extract_docx_text(file: UploadFile) -> str:
    doc = docx.Document(io.BytesIO(await file.read()))
    text = ""
    for para in doc.paragraphs:
        text += para.text + "\n"
    return text


# Function to extract text from CSV file
async def extract_csv_text(file: UploadFile) -> str:
    df = pd.read_csv(io.BytesIO(await file.read()))

    def row_to_string(row):
        return ", ".join([f"{col}: {value}" for col, value in row.items()])

    data = df.apply(row_to_string, axis=1).tolist()
    return data


async def get_csv_query_type(doc_data, query):
    content = doc_data.get("content", {})

    if not {"rowWiseData", "columnWiseData"}.issubset(content):
        return "rowWiseData" if "rowWiseData" in content else "columnWiseData"

    user = "Tell me the query type"
    variables = {"headers": doc_data["content"]["headers"], "query": query}
    response = await call_ai_middleware(user, bridge_id=bridge_ids["get_csv_query_type"], variables=variables)
    query_type = response["search"]
    return "columnWiseData" if query_type == "column" else "rowWiseData"
