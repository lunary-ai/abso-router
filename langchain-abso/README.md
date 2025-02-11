# langchain-abso

This package contains the LangChain integration with Abso

## Installation

```bash
pip install -U langchain-abso
```

You need to set the environment variable `OPENAI_API_KEY` to use Abso.


## Chat Models

`ChatAbso` class exposes chat models from Abso.

```python
from langchain_abso import ChatAbso

llm = ChatAbso(fast_model="gpt-4o", slow_model="o3-mini")
llm.invoke("Sing a ballad of LangChain.")
```
