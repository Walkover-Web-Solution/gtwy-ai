from fastapi import HTTPException
from fastapi.responses import JSONResponse

from config import Config
from ..schemas.rag_schemas import RAGQueryRequest
from ..services.utils.apiservice import fetch


async def get_vectors_and_text(body: RAGQueryRequest):
    try:
        query = body.query
        top_k = body.top_k
        score = body.score
        collection_id = body.collection_id
        owner_id = body.owner_id
        resource_id = body.doc_id or body.resource_id

        if collection_id and owner_id:
            resource_id = "collection_only_query"
            resource_to_collection_mapping = {resource_id: collection_id}
        else:
            hippocampus_resource_url = f"http://hippocampus.gtwy.ai/resource/{resource_id}"
            headers = {"x-api-key": Config.HIPPOCAMPUS_API_KEY}

            resource_response, _ = await fetch(url=hippocampus_resource_url, method="GET", headers=headers)

            owner_id = resource_response.get("ownerId")
            collection_id = resource_response.get("collectionId")

            if not owner_id:
                raise HTTPException(status_code=400, detail="Owner ID not found in resource response.")

            resource_to_collection_mapping = {}
            if collection_id:
                resource_to_collection_mapping[resource_id] = collection_id

        text = await get_text_from_vectorsQuery(
            {"resource_id": resource_id, "query": query, "top_k": top_k, "minScore": score},
            Flag=False,
            score=score,
            owner_id=owner_id,
            resource_to_collection_mapping=resource_to_collection_mapping,
        )

        success = text.get("status") == 1
        status_code = 200 if success else 400

        api_response = text.get("response", {})
        results = api_response.get("result", [])
        merged_text = text.get("merged_text", "")

        return JSONResponse(
            status_code=status_code,
            content={
                "success": success,
                "text": merged_text,
                "results": results
            }
        )

    except Exception as error:
        print(f"Error in get_vectors_and_text: {error}")
        raise HTTPException(status_code=400, detail=str(error)) from error


async def get_text_from_vectorsQuery(args, Flag=True, score=0.1, owner_id=None, resource_to_collection_mapping=None):
    try:
        query = args.get("query")
        top_k = args.get("top_k", 3)
        min_score = args.get("minScore", score)
        ownerId = owner_id
        resource_id = args.get("resource_id")

        if query is None:
            raise HTTPException(status_code=400, detail="Query is required.")

        if min_score < 0.1 or min_score > 1:
            raise HTTPException(status_code=400, detail="minScore must be between 0.1 and 1.")

        if not resource_to_collection_mapping:
            resource_to_collection_mapping = {}

        collection_id = resource_to_collection_mapping.get(resource_id)

        hippocampus_url = "http://hippocampus.gtwy.ai/search"
        headers = {"x-api-key": Config.HIPPOCAMPUS_API_KEY, "Content-Type": "application/json"}

        payload = {"query": query, "ownerId": ownerId, "minScore": min_score, "top_k": top_k}

        is_collection_only_query = resource_id and resource_id == "collection_only_query" and collection_id

        if is_collection_only_query:
            payload["collectionId"] = collection_id
        elif resource_id:
            payload["resourceId"] = resource_id
            if collection_id:
                payload["collectionId"] = collection_id
        else:
            raise Exception("Either Resource ID or Collection ID must be provided.")

        api_response, response_headers = await fetch(
            url=hippocampus_url, method="POST", headers=headers, json_body=payload
        )

        results = api_response.get("result", [])
        merged_text = ""
        for result in results:
            payload_data = result.get("payload", {})
            content = payload_data.get("content", "")
            merged_text += content + "\n"

        if Flag:
            return {
                "response": merged_text.strip(),
                "metadata": {"type": "RAG"},
                "status": 1
            }
        else:
            return {
                "response": api_response,
                "merged_text": merged_text.strip(),
                "metadata": {"type": "RAG"},
                "status": 1
            }

    except Exception as error:
        return {
            "response": str(error),
            "metadata": {"type": "RAG"},
            "status": 0,
        }
