from typing import Any, Dict, Iterator, List, Optional, Union, Mapping, cast
import requests
import os
import json


from langchain_core.output_parsers.openai_tools import (
    JsonOutputKeyToolsParser,
    PydanticToolsParser,
    make_invalid_tool_call,
    parse_tool_call,
)

from langchain_core.callbacks import (
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    InvalidToolCall,
    ChatMessage,
    HumanMessage,
    ToolCall,
    ToolMessage,
    SystemMessage,
    FunctionMessage

)
from langchain_core.utils.utils import secret_from_env
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.messages.ai import (
    InputTokenDetails,
    OutputTokenDetails,
    UsageMetadata,
)
from pydantic import SecretStr, Field



def _format_message_content(content: Any) -> Any:
    """Format message content."""
    if content and isinstance(content, list):
        # Remove unexpected block types
        formatted_content = []
        for block in content:
            if (
                isinstance(block, dict)
                and "type" in block
                and block["type"] == "tool_use"
            ):
                continue
            else:
                formatted_content.append(block)
    else:
        formatted_content = content

    return formatted_content

def _convert_message_to_dict(message: BaseMessage) -> dict:
    """Convert a LangChain message to a dictionary.

    Args:
        message: The LangChain message.

    Returns:
        The dictionary.
    """
    message_dict: Dict[str, Any] = {"content": _format_message_content(message.content)}
    if (name := message.name or message.additional_kwargs.get("name")) is not None:
        message_dict["name"] = name

    # populate role and additional message data
    if isinstance(message, ChatMessage):
        message_dict["role"] = message.role
    elif isinstance(message, HumanMessage):
        message_dict["role"] = "user"
    elif isinstance(message, AIMessage):
        message_dict["role"] = "assistant"
        if "function_call" in message.additional_kwargs:
            message_dict["function_call"] = message.additional_kwargs["function_call"]
        if message.tool_calls or message.invalid_tool_calls:
            message_dict["tool_calls"] = [
                _lc_tool_call_to_openai_tool_call(tc) for tc in message.tool_calls
            ] + [
                _lc_invalid_tool_call_to_openai_tool_call(tc)
                for tc in message.invalid_tool_calls
            ]
        elif "tool_calls" in message.additional_kwargs:
            message_dict["tool_calls"] = message.additional_kwargs["tool_calls"]
            tool_call_supported_props = {"id", "type", "function"}
            message_dict["tool_calls"] = [
                {k: v for k, v in tool_call.items() if k in tool_call_supported_props}
                for tool_call in message_dict["tool_calls"]
            ]
        else:
            pass
        # If tool calls present, content null value should be None not empty string.
        if "function_call" in message_dict or "tool_calls" in message_dict:
            message_dict["content"] = message_dict["content"] or None

        if "audio" in message.additional_kwargs:
            raw_audio = message.additional_kwargs["audio"]
            audio = (
                {"id": message.additional_kwargs["audio"]["id"]}
                if "id" in raw_audio
                else raw_audio
            )
            message_dict["audio"] = audio
    elif isinstance(message, SystemMessage):
        message_dict["role"] = message.additional_kwargs.get(
            "__openai_role__", "system"
        )
    elif isinstance(message, FunctionMessage):
        message_dict["role"] = "function"
    elif isinstance(message, ToolMessage):
        message_dict["role"] = "tool"
        message_dict["tool_call_id"] = message.tool_call_id

        supported_props = {"content", "role", "tool_call_id"}
        message_dict = {k: v for k, v in message_dict.items() if k in supported_props}
    else:
        raise TypeError(f"Got unknown type {message}")
    return message_dict


def _convert_dict_to_message(_dict: Mapping[str, Any]) -> BaseMessage:
    """Convert a dictionary to a LangChain message.

    Args:
        _dict: The dictionary.

    Returns:
        The LangChain message.
    """
    role = _dict.get("role")
    name = _dict.get("name")
    id_ = _dict.get("id")
    if role == "user":
        return HumanMessage(content=_dict.get("content", ""), id=id_, name=name)
    elif role == "assistant":
        # Fix for azure
        # Also OpenAI returns None for tool invocations
        content = _dict.get("content", "") or ""
        additional_kwargs: Dict = {}
        if function_call := _dict.get("function_call"):
            additional_kwargs["function_call"] = dict(function_call)
        tool_calls = []
        invalid_tool_calls = []
        if raw_tool_calls := _dict.get("tool_calls"):
            additional_kwargs["tool_calls"] = raw_tool_calls
            for raw_tool_call in raw_tool_calls:
                try:
                    tool_calls.append(parse_tool_call(raw_tool_call, return_id=True))
                except Exception as e:
                    invalid_tool_calls.append(
                        make_invalid_tool_call(raw_tool_call, str(e))
                    )
        if audio := _dict.get("audio"):
            additional_kwargs["audio"] = audio
        return AIMessage(
            content=content,
            additional_kwargs=additional_kwargs,
            name=name,
            id=id_,
            tool_calls=tool_calls,
            invalid_tool_calls=invalid_tool_calls,
        )
    elif role in ("system", "developer"):
        if role == "developer":
            additional_kwargs = {"__openai_role__": role}
        else:
            additional_kwargs = {}
        return SystemMessage(
            content=_dict.get("content", ""),
            name=name,
            id=id_,
            additional_kwargs=additional_kwargs,
        )
    elif role == "function":
        return FunctionMessage(
            content=_dict.get("content", ""), name=cast(str, _dict.get("name")), id=id_
        )
    elif role == "tool":
        additional_kwargs = {}
        if "name" in _dict:
            additional_kwargs["name"] = _dict["name"]
        return ToolMessage(
            content=_dict.get("content", ""),
            tool_call_id=cast(str, _dict.get("tool_call_id")),
            additional_kwargs=additional_kwargs,
            name=name,
            id=id_,
        )
    else:
        return ChatMessage(content=_dict.get("content", ""), role=role, id=id_)  # type: ignore[arg-type]


def _lc_invalid_tool_call_to_openai_tool_call(
    invalid_tool_call: InvalidToolCall,
) -> dict:
    return {
        "type": "function",
        "id": invalid_tool_call["id"],
        "function": {
            "name": invalid_tool_call["name"],
            "arguments": invalid_tool_call["args"],
        },
    }

def _lc_tool_call_to_openai_tool_call(tool_call: ToolCall) -> dict:
    return {
        "type": "function",
        "id": tool_call["id"],
        "function": {
            "name": tool_call["name"],
            "arguments": json.dumps(tool_call["args"]),
        },
    }


def _create_usage_metadata(oai_token_usage: dict) -> UsageMetadata:
    input_tokens = oai_token_usage.get("prompt_tokens", 0)
    output_tokens = oai_token_usage.get("completion_tokens", 0)
    total_tokens = oai_token_usage.get("total_tokens", input_tokens + output_tokens)
    input_token_details: dict = {
        "audio": (oai_token_usage.get("prompt_tokens_details") or {}).get(
            "audio_tokens"
        ),
        "cache_read": (oai_token_usage.get("prompt_tokens_details") or {}).get(
            "cached_tokens"
        ),
    }
    output_token_details: dict = {
        "audio": (oai_token_usage.get("completion_tokens_details") or {}).get(
            "audio_tokens"
        ),
        "reasoning": (oai_token_usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        ),
    }
    return UsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        input_token_details=InputTokenDetails(
            **{k: v for k, v in input_token_details.items() if v is not None}
        ),
        output_token_details=OutputTokenDetails(
            **{k: v for k, v in output_token_details.items() if v is not None}
        ),
    )


def _create_chat_result(
    response: Union[dict, Any],
    generation_info: Optional[Dict] = None,
) -> ChatResult:
    generations = []

    response_dict = (
        response if isinstance(response, dict) else response.model_dump()
    )
    if response_dict.get("error"):
        raise ValueError(response_dict.get("error"))

    token_usage = response_dict.get("usage")
    for res in response_dict["choices"]:
        message = _convert_dict_to_message(res["message"])
        if token_usage and isinstance(message, AIMessage):
            message.usage_metadata = _create_usage_metadata(token_usage)
        generation_info = generation_info or {}
        generation_info["finish_reason"] = (
            res.get("finish_reason")
            if res.get("finish_reason") is not None
            else generation_info.get("finish_reason")
        )
        if "logprobs" in res:
            generation_info["logprobs"] = res["logprobs"]
        gen = ChatGeneration(message=message, generation_info=generation_info)
        generations.append(gen)
    llm_output = {
        "token_usage": token_usage,
        "model_name": response_dict.get("model"),
        "system_fingerprint": response_dict.get("system_fingerprint", ""),
    }

    if getattr(response, "choices", None):
        message = response.choices[0].message  # type: ignore[attr-defined]
        if hasattr(message, "parsed"):
            generations[0].message.additional_kwargs["parsed"] = message.parsed
        if hasattr(message, "refusal"):
            generations[0].message.additional_kwargs["refusal"] = message.refusal

    return ChatResult(generations=generations, llm_output=llm_output)


class ChatAbso(BaseChatModel):
    """A smart LLM proxy that automatically routes requests between fast and slow models based on prompt complexity. 
    It uses several heuristics to determine the complexity of the prompt and routes the request to the appropriate model.
    The model used can be specified by the user. 
    It only supports openai models for now, but will support other models in the future.
    You need to have an OpenAI API key to use this model.

    .. dropdown:: Setup
        :open:
        Set then environment variable ``OPENAI_API_KEY``.

        .. code-block:: bash

            pip install -U langchain-openai
            export OPENAI_API_KEY="your-api-key"

    .. dropdown:: Usage
    Example:
        .. code-block:: python
            abso = ChatAbso(fast_model="gpt-4o", slow_model="o3-mini")
            result = abso.invoke([HumanMessage(content="hello")])
            result = model.batch([[HumanMessage(content="What is the meaning of life?")]
    """

    fast_model: str 
    """The identifier of the fast model used for simple or lower-complexity tasks, ensuring quick response times."""
    slow_model: str
    """The identifier of the slow model used for complex or high-accuracy tasks where thorough processing is needed."""
    openai_api_key: Optional[SecretStr] = Field(alias="api_key", default_factory=secret_from_env("OPENAI_API_KEY", default=None))
    temperature: Optional[float] = None
    """What sampling temperature to use."""
    presence_penalty: Optional[float] = None
    """Penalizes repeated tokens."""
    frequency_penalty: Optional[float] = None
    """Penalizes repeated tokens according to frequency."""
    seed: Optional[int] = None
    """Seed for generation"""
    logprobs: Optional[bool] = None
    """Whether to return logprobs."""
    top_logprobs: Optional[int] = None
    """Number of most likely tokens to return at each token position, each with
     an associated log probability. `logprobs` must be set to true 
     if this parameter is used."""
    logit_bias: Optional[Dict[int, int]] = None
    """Modify the likelihood of specified tokens appearing in the completion."""
    streaming: bool = False
    """Whether to stream the results or not."""
    n: Optional[int] = None
    """Number of chat completions to generate for each prompt."""
    top_p: Optional[float] = None
    """Total probability mass of tokens to consider at each step."""
    max_tokens: Optional[int] = Field(default=None)
    """Maximum number of tokens to generate."""
    reasoning_effort: Optional[str] = None
    """Constrains effort on reasoning for reasoning models.""" 
    stop: Optional[Union[List[str], str]] = Field(default=None, alias="stop_sequences")
    """Default stop sequences."""


    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        **kwargs
    ) -> ChatResult:
        """
        Args:
            messages: the prompt composed of a list of messages.
            stop: a list of stop tokens that should be respected and, if triggered,
                must be included as part of the final output.
        """
        payload = {
            "messages": [_convert_message_to_dict(message) for message in messages],
            "fastModel": self.fast_model,
            "slowModel": self.slow_model,
            "stream": False,
        }
        if self.stop is not None or stop is not None:
            payload["stop"] = stop

        headers = {
            'Authorization': f'Bearer {os.environ.get("OPENAI_API_KEY")}',
            'Content-Type': 'application/json'
        }

        response = requests.post(
            'http://localhost:8787/v1/chat/completions',
            json=payload,
            headers=headers,
        )

        generation_info = {"headers": dict(response.headers)}
        result = _create_chat_result(response.json(), generation_info)

        if stop is not None and result.generations:
            stop_token = stop[0] if isinstance(stop, list) else stop
            for generation in result.generations:
                if generation.generation_info.get("finish_reason") == "stop":
                    content = generation.message.content or ""
                    if not content.endswith(stop_token):
                        generation.message.content = content + stop_token

        return result
    @property
    def _llm_type(self) -> str:
        """Get the type of language model used by this chat model."""
        return "openai-chat"
