from langchain_openai import ChatOpenAI

from graph.state import AgentState

DIRECT_SYSTEM_PROMPT = """You are a helpful AI assistant. Answer the user's request directly, thoroughly, and with real output. If they ask for code, write the code. If they ask for text, write the text. Be specific and actionable."""


async def direct_node(state: AgentState) -> dict:
    """Direct mode — single LLM call, no planning. Streams via astream_events."""
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        api_key=state["api_key"],
        temperature=0.5,
    )

    response = await llm.ainvoke(
        [
            {"role": "system", "content": DIRECT_SYSTEM_PROMPT},
            {"role": "user", "content": state["goal"]},
        ]
    )

    return {"final_answer": response.content}
