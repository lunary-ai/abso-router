from model import ChatAbso
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage
)


abso = ChatAbso(fast_model="gpt-4o", slow_model="o3-mini")
res = abso.invoke([HumanMessage(content="hello")])
print(res.content)
res = abso.invoke([HumanMessage(content="what's the meaning of life")])
print(res.content)