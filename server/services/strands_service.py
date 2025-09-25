"""
Strands Agent Service
统一的 AWS Strands Agent 服务，支持单agent和多agent模式
"""
import asyncio
import json
import traceback
from typing import List, Dict, Any, Optional

from strands import Agent, tool
try:
    from strands.models import BedrockModel
    from strands.models.openai import OpenAIModel
    from strands.models.anthropic import AnthropicModel
    from strands.models.ollama import OllamaModel
except ImportError:
    from strands.models import BedrockModel
    # 如果导入失败，使用BedrockModel作为fallback
    try:
        from strands.models.openai import OpenAIModel
    except ImportError:
        OpenAIModel = BedrockModel
    try:
        from strands.models.anthropic import AnthropicModel
    except ImportError:
        AnthropicModel = BedrockModel
    try:
        from strands.models.ollama import OllamaModel
    except ImportError:
        OllamaModel = BedrockModel

from services.db_service import db_service
from services.config_service import config_service
from services.websocket_service import send_to_websocket, send_to_user_websocket
from services.strands_context import SessionContextManager, set_intention_result
from services.user_context import get_current_user_id
from pydantic import Field

from tools.strands_intention import analyze_edit_intention




def _process_message_content_for_agent(content):
    """
    处理消息内容，过滤掉图像消息或将其转换为文本描述
    确保传递给Agent的内容是纯文本格式
    """
    if isinstance(content, str):
        # 如果是字符串，检查是否包含图像引用
        if '/api/file/' in content and ('im_' in content or '.jpg' in content or '.png' in content):
            # 这是包含图像引用的消息，转换为文本描述
            return "[图像消息]"
        return content

    elif isinstance(content, list):
        # 如果是列表，过滤掉图像内容，保留文本内容
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get('type') == 'text' and 'text' in item:
                    text_parts.append(item['text'])
                elif item.get('type') == 'image_url':
                    text_parts.append("[图像]")
            elif isinstance(item, str):
                text_parts.append(item)

        return ' '.join(text_parts) if text_parts else None

    else:
        # 其他类型，尝试转换为字符串
        return str(content) if content else None


# 全局变量来跟踪已发送的事件，防止重复
_sent_events = set()


# Map toolUseId to tool name for better logging
_tool_call_names = {}
# Track current tool use per session for better logging/id propagation
_current_tool_use_by_session = {}



async def send_user_websocket_message(session_id: str, event: dict):
    """Send WebSocket message to the current user"""
    try:
        user_id = get_current_user_id()
        await send_to_user_websocket(session_id, event, user_id)
    except Exception as e:
        # Fallback to broadcast if user context is not available
        print(f"⚠️ User context not available, falling back to broadcast: {e}")
        await send_to_websocket(session_id, event)


async def handle_image_generation_result(tool_result_text: str, session_id: str, tool_call_id: str):
    """处理图像生成工具的结果，如果检测到图像生成成功，则保存图像消息"""
    try:
        print(f"🔍 DEBUG: Checking tool result text: {tool_result_text[:200]}...")

        # 检查是否是图像生成成功的消息
        if "Image generated successfully!" in tool_result_text and "File ID:" in tool_result_text:
            print(f"🎨 DEBUG: Found image generation success message")

            # 提取文件ID
            import re
            file_id_match = re.search(r'File ID: ([^,\s]+)', tool_result_text)
            print(f"🔍 DEBUG: Regex match result: {file_id_match}")

            if file_id_match:
                file_id = file_id_match.group(1)
                print(f"🎨 DEBUG: Detected image generation result, file_id: {file_id}")

                # 创建图像消息格式
                image_message = {
                    'role': 'assistant',
                    'content': [
                        {
                            'type': 'image_url',
                            'image_url': {
                                'url': f'/api/file/{file_id}'
                            }
                        }
                    ]
                }

                # 保存图像消息到数据库
                try:
                    db_service.create_message(session_id, 'assistant', json.dumps(image_message))
                    print(f"✅ Saved image message for file_id: {file_id}")
                except Exception as save_error:
                    print(f"❌ ERROR: Failed to save image message: {save_error}")
                    traceback.print_exc()
            else:
                print(f"❌ DEBUG: Failed to extract file_id from: {tool_result_text}")
        # 检查是否是视频生成成功的消息
        elif "Video generated successfully!" in tool_result_text and "File ID:" in tool_result_text:
            # 提取文件ID
            import re
            file_id_match = re.search(r'File ID: `([^`]+)`', tool_result_text)
            if file_id_match:
                file_id = file_id_match.group(1)
                print(f"🎬 DEBUG: Detected video generation result, file_id: {file_id}")

                # 对于视频，我们保存包含下载链接的文本消息（因为前端还没有专门的视频消息组件）
                # 这里我们不需要额外保存，因为工具返回的文本消息已经包含了下载链接
                print(f"✅ Video message will be saved as text with download link")

    except Exception as e:
        print(f"⚠️ Error handling generation result: {e}")
        # 不抛出异常，避免影响主流程




def _extract_first_json_object(text: str):
    """Extract the first top-level JSON object from a text stream.
    Handles leading/trailing non-JSON text and ignores braces inside strings.
    Returns a parsed dict if found, else None.
    """
    try:
        in_str = False
        esc = False
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if esc:
                esc = False
                continue
            if ch == '\\':
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start != -1:
                        candidate = text[start:i+1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            # continue searching if this candidate fails
                            pass
        return None
    except Exception:
        return None

def create_model_instance(text_model: Dict[str, Any]):
    """创建模型实例"""
    model = text_model.get('model')
    provider = text_model.get('provider')
    url = text_model.get('url')
    api_key = config_service.app_config.get(provider, {}).get("api_key", "")
    max_tokens = text_model.get('max_tokens', 8148)

    if provider == 'ollama':
        try:
            return OllamaModel(
                model=model,
                base_url=url,
            )
        except:
            return BedrockModel(model_id=model)
    elif provider == 'bedrock':
        region = config_service.app_config.get(provider, {}).get("region", "us-west-2")
        return BedrockModel(
            model_id=model,
            region_name=region,
            max_tokens=max_tokens,
            temperature=0
        )
    elif provider == 'anthropic':
        try:
            return AnthropicModel(
                model=model,
                api_key=api_key,
                max_tokens=max_tokens,
                temperature=0
            )
        except:
            return BedrockModel(model_id=model)
    elif provider == 'siliconflow':
        try:
            return OpenAIModel(
                client_args={
                    "api_key": api_key,
                    "base_url": url
                },
                model_id=model,
                max_tokens=max_tokens,
                temperature=0.7
            )
        except Exception as e:
            print(f"❌ Failed to create OpenAIModel for siliconflow: {e}")
            # 不要降级到BedrockModel，因为deepseek模型ID在Bedrock中无效
            raise Exception(f"Failed to create siliconflow model: {e}")
    else:
        try:
            return OpenAIModel(
                model=model,
                api_key=api_key,
                base_url=url,
                temperature=0,
                max_tokens=max_tokens,
            )
        except:
            return BedrockModel(model_id=model)


def get_specialized_agents():
    """获取专门化agent工具列表"""
    try:
        from tools.strands_specialized_agents import get_specialized_agents

        agents = get_specialized_agents()
        print(f"✅ Loaded {len(agents)} specialized agents")
        for agent in agents:
            print(f"  - {agent.__name__}: {type(agent)}")
        return agents
    except Exception as e:
        print(f"❌ Failed to load specialized agents: {e}")
        traceback.print_exc()
        return []


async def strands_agent(messages, canvas_id, session_id, text_model, image_model, video_model=None, system_prompt: str = None):
    """单个 Strands Agent 处理"""
    try:
        model = create_model_instance(text_model)

        # 创建系统提示
        # Detect availability of use_agent tool early to shape prompt and tool list
        has_use_agent = False
        use_agent_tool = None
        try:
            from strands_tools import use_agent as _use_agent
            use_agent_tool = _use_agent
            has_use_agent = True
            print("✅ use_agent available; will prefer it for intent classification")
        except Exception as e:
            print(f"⚠️ use_agent not available, will use analyze_edit_intention as fallback: {e}")

        available_tools = []

        # 检查是否使用 ComfyUI 模型
        is_comfyui_model = (
            image_model.get('provider') == 'comfyui' or
            (video_model and video_model.get('provider') == 'comfyui')
        )

        print(f"🔍 DEBUG: is_comfyui_model = {is_comfyui_model}")
        print(f"🔍 DEBUG: image_model.provider = {image_model.get('provider')}")
        print(f"🔍 DEBUG: video_model = {video_model}")
        if video_model:
            print(f"🔍 DEBUG: video_model.provider = {video_model.get('provider')}")

        # Always expose contextual tools directly
        available_tools.append("generate_image_with_context: Generate images based on text descriptions")
        available_tools.append("generate_video_with_context: Generate videos based on text descriptions and optionally input images")

        # Intent classification is executed BEFORE this agent via a child agent.
        # The resulting generation_model is available via session context; tools will fall back to it if model_override is omitted.


        tools_description = "\n".join([f"- {tool}" for tool in available_tools])

        agent_system_prompt = system_prompt or f"""
You are a professional AI assistant with image and video generation capabilities.

Available tools:
{tools_description}

When users request image or video generation:
1. Intent classification has ALREADY been performed BEFORE this agent run by a dedicated child agent. Do NOT call use_agent again. Use the provided generation_model by passing it via model_override when invoking generation tools. If model_override is omitted, tools will fall back to the session's generation_model (if available).
2. Create a detailed, descriptive prompt for the content
3. Use the appropriate generation tool:
   - If generation_model starts with 'flux-': call generate_image_with_context
   - If generation_model starts with 'wan-': call generate_video_with_context
4. Always pass the detected generation_model via model_override when calling generation tools. Valid values:
   - flux-kontext (single image edit)
   - flux-kontext-multiple (multiple image edit)
   - flux-t2i (text-to-image)
   - wan-t2i (text-to-video)
   - wan-i2v (image-to-video)
   Important: Explicitly pass model_override=generation_model for precision and traceability.
5. Choose appropriate aspect ratios based on the content
6. The tools support both text-to-image/video and image-to-image/video modes
7. For I2I/I2V, you can specify an input_image or use use_previous_image=True
8. For each single user request/turn, call at most ONE generation tool (image or video). Do NOT call generation tools multiple times in the same turn unless the user explicitly asks to refine or generate another version. After receiving the first successful tool result, provide the final answer and stop further tool calls in this turn.



Intent classification (reference only):
Below is the STRICT JSON template the child agent uses when classifying intent (for your awareness; you should not re-run it):

- You are an intent classifier for image editing requests.
- Output STRICT JSON only, no extra text.
- Fields:
  - mode: "single_img_edit" | "multiple_img_edit" | "text_to_image" | "text_to_video" | "image_to_video"
  - generation_model: one of "flux-kontext", "flux-kontext-multiple", "flux-t2i", "wan-t2i", "wan-i2v"
  - reasoning: short Chinese explanation
- Rules:
  - Analyze ONLY the user's intent from their request text, ignore whether images are already uploaded
  - If user wants to edit/modify/add elements to an existing image → output "single"
  - If user explicitly wants to merge/combine/blend/fuse multiple images → output "multiple"
  - If user wants to create completely new content without referencing existing images → output "text_only"
- Return ONLY JSON, with no leading/trailing commentary.

IMPORTANT - Context Usage:
- Image tool has use_previous_image=True by default, which automatically uses the most recent image from this conversation
- Video tool has use_previous_image=True by default, which automatically uses the most recent image for I2V generation
- Use use_previous_image=TRUE when the user wants to EDIT, MODIFY, or BUILD UPON an existing image/video
- Use use_previous_image=FALSE when the user wants a COMPLETELY NEW, UNRELATED content or explicitly asks for a "new" creation
- IMPORTANT: Some models (like flux-t2i, wan-t2v) are text-only models and don't support input images. The tools will automatically ignore use_previous_image for these models
- If no previous image exists in the conversation, the tools will inform you appropriately

For other tasks, use your general knowledge and reasoning capabilities.
Be helpful, accurate, and creative in your responses.
"""

        # 获取历史消息并转换为Strands格式
        try:
            # 获取数据库中的历史消息
            historical_messages = db_service.get_chat_history(session_id)
            print(f"🔍 DEBUG: Retrieved {len(historical_messages)} historical messages from database")

            # 将当前传入的messages与历史消息合并，去重
            all_messages = []

            # 添加历史消息，过滤掉图像内容
            for hist_msg in historical_messages:
                if hist_msg.get('role') and hist_msg.get('content'):
                    # 处理content，过滤掉图像消息或转换为文本描述
                    content = hist_msg['content']
                    processed_content = _process_message_content_for_agent(content)

                    if processed_content:  # 只添加有效的文本内容
                        # 转换为Bedrock标准消息格式
                        all_messages.append({
                            'role': hist_msg['role'],
                            'content': [
                                {
                                    'text': processed_content
                                }
                            ]
                        })

            # 添加当前消息（如果不在历史中）
            for current_msg in messages:
                if current_msg.get('role') and current_msg.get('content'):
                    # 简单去重：检查最后几条消息是否已存在
                    is_duplicate = False
                    for existing_msg in all_messages[-3:]:  # 只检查最后3条避免性能问题
                        if (existing_msg.get('role') == current_msg.get('role') and
                            existing_msg.get('content') == current_msg.get('content')):
                            is_duplicate = True
                            break

                    if not is_duplicate:
                        # 转换当前消息为Bedrock标准格式
                        content = current_msg['content']
                        if isinstance(content, str):
                            formatted_content = [{'text': content}]
                        elif isinstance(content, list):
                            # 转换列表格式，确保使用Bedrock格式
                            formatted_content = []
                            for item in content:
                                if isinstance(item, dict):
                                    if item.get('type') == 'text' and 'text' in item:
                                        formatted_content.append({'text': item['text']})
                                    elif 'text' in item and 'type' not in item:
                                        formatted_content.append({'text': item['text']})
                                    elif isinstance(item.get('text'), str):
                                        formatted_content.append({'text': item['text']})
                                elif isinstance(item, str):
                                    formatted_content.append({'text': item})
                            if not formatted_content:
                                formatted_content = [{'text': str(content)}]
                        else:
                            formatted_content = [{'text': str(content)}]

                        all_messages.append({
                            'role': current_msg['role'],
                            'content': formatted_content
                        })

            print(f"🔍 DEBUG: Total messages for agent: {len(all_messages)}")

            # 获取最后一条用户消息作为当前prompt
            user_prompt = ""
            for msg in reversed(all_messages):
                if msg.get('role') == 'user':
                    user_prompt = msg.get('content', '')
                    break

            if not user_prompt:
                user_prompt = "Hello, how can I help you?"

        except Exception as e:
            print(f"❌ Error retrieving chat history: {e}")
            # 降级处理：只使用当前传入的消息
            all_messages = []
            for msg in messages:
                if msg.get('role') and msg.get('content'):
                    # 转换为Bedrock标准格式
                    content = msg['content']
                    if isinstance(content, str):
                        formatted_content = [{'text': content}]
                    elif isinstance(content, list):
                        formatted_content = content  # 假设已经是正确格式
                    else:
                        formatted_content = [{'text': str(content)}]

                    all_messages.append({
                        'role': msg['role'],
                        'content': formatted_content
                    })

            user_prompt = ""
            for msg in reversed(messages):
                if msg.get('role') == 'user':
                    user_prompt = msg.get('content', '')
                    break

            if not user_prompt:
                user_prompt = "Hello, how can I help you?"

        # 使用上下文管理器，传递当前用户ID
        try:
            current_user_id = get_current_user_id()
        except Exception:
            current_user_id = None

        # 准备模型上下文
        model_context = {'image': image_model}
        if video_model:
            model_context['video'] = video_model

        with SessionContextManager(session_id, canvas_id, model_context, user_id=current_user_id):
            print(f"💬 Processing: {user_prompt[:50]}...")

            # 创建带有上下文信息的工具
            tools = []
            # Run child intent agent BEFORE building main tools: isolate use_agent and store result in session context
            try:
                intent_set = False
                if has_use_agent and use_agent_tool:
                    child_system_prompt = (
                        "You are an intent classifier for image editing/generation.\n"
                        "Output STRICT JSON only, no extra text.\n"
                        "Fields:\n"
                        "  - mode: \"single_img_edit\" | \"multiple_img_edit\" | \"text_to_image\" | \"text_to_video\" | \"image_to_video\"\n"
                        "  - generation_model: one of \"flux-kontext\", \"flux-kontext-multiple\", \"flux-t2i\", \"wan-t2i\", \"wan-i2v\"\n"
                        "  - reasoning: short Chinese explanation\n"
                        "Rules:\n"
                        "  - Analyze ONLY the user's current request text (no tool usage besides use_agent).\n"
                        "  - Do not include any additional commentary. JSON only.\n"
                        "  - Start your response with '{' (left brace). Do NOT use code fences or backticks.\n"
                    )

                    # Tighten child model to avoid code fences; add stop sequences when using Bedrock
                    child_model = model
                    try:
                        if isinstance(model, BedrockModel):
                            region = config_service.app_config.get('bedrock', {}).get('region', 'us-west-2')
                            child_model = BedrockModel(
                                model_id=text_model.get('model'),
                                region_name=region,
                                max_tokens=text_model.get('max_tokens', 8192),
                                temperature=0,
                                stop_sequences=["```", "```json"]
                            )
                    except Exception as _e:
                        print(f"⚠️ Failed to create tightened child model: {_e}")
                        child_model = model

                    child_agent = Agent(
                        model=child_model,
                        tools=[],  # no tools to avoid recursive use_agent calls
                        system_prompt=child_system_prompt
                    )

                    parsed_result = None
                    buffer = ""
                    async for ev in child_agent.stream_async(user_prompt):
                        if isinstance(ev, dict) and 'event' in ev and 'contentBlockDelta' in ev['event']:
                            delta = ev['event']['contentBlockDelta']['delta']
                            if 'text' in delta:
                                buffer += delta['text']
                                candidate = _extract_first_json_object(buffer)
                                if isinstance(candidate, dict):
                                    parsed_result = candidate
                                    break
                        elif isinstance(ev, str):
                            buffer += ev
                            candidate = _extract_first_json_object(buffer)
                            if isinstance(candidate, dict):
                                parsed_result = candidate
                                break
                        # If the SDK provides a messageStop or similar, we could break there and parse once
                        # For now we incrementally try to parse as JSON as text accumulates
                        if parsed_result is not None:
                            break

                    if isinstance(parsed_result, dict):
                        set_intention_result(parsed_result)
                        mode = parsed_result.get('mode'); gm = parsed_result.get('generation_model')
                        print(f"🧭 [child] use_agent parsed → mode={mode}, generation_model={gm}")
                        intent_set = True
                    else:
                        print("⚠️ [child] use_agent did not produce parsable JSON; will fallback to keyword intention tool")

                # Fallback to local keyword tool if needed or if use_agent unavailable
                if not intent_set:
                    try:
                        kw = analyze_edit_intention(prompt=user_prompt)
                        try:
                            parsed = json.loads(kw) if isinstance(kw, str) else None
                        except Exception:
                            parsed = None
                        if isinstance(parsed, dict):
                            set_intention_result(parsed)
                            mode = parsed.get('mode'); gm = parsed.get('generation_model')
                            print(f"🧭 [fallback] analyze_edit_intention → mode={mode}, generation_model={gm}")
                        else:
                            print("⚠️ analyze_edit_intention returned non-JSON; intention not stored")
                    except Exception as _ie:
                        print(f"⚠️ Failed to run intention fallback: {_ie}")
            except Exception as _child_e:
                print(f"⚠️ Child intent agent error: {_child_e}")


            # Intent classification is executed by a child agent BEFORE main agent; do not register intent tools here
            print("🧭 Intent pre-classified by child agent; generation tools only in main agent")


            # Always use contextual tools directly (remove smart ComfyUI router)
            from tools.strands_image_generators import create_generate_image_with_context
            contextual_generate_image = create_generate_image_with_context(session_id, canvas_id, image_model, current_user_id)
            tools.append(contextual_generate_image)

            # Always add video generation tool (use default if frontend didn't provide)
            from tools.strands_video_generators import create_generate_video_with_context
            _video_model = video_model if video_model else {"provider": "comfyui", "model": "wan-t2v"}
            contextual_generate_video = create_generate_video_with_context(session_id, canvas_id, _video_model, current_user_id)
            tools.append(contextual_generate_video)

            print(f"🔍 DEBUG: Using tools: {[tool.__name__ for tool in tools]}")

            # 创建带有上下文工具的agent，并设置历史消息
            agent = Agent(
                model=model,
                tools=tools,
                system_prompt=agent_system_prompt
            )

            # 设置历史消息到agent中，让模型能够访问完整的对话上下文
            if len(all_messages) > 1:  # 如果有历史消息
                # 将历史消息转换为Strands Agent期望的格式
                agent.messages = all_messages[:-1]  # 除了最后一条用户消息，其他都作为历史
                print(f"🔍 DEBUG: Set {len(agent.messages)} historical messages to agent")

                # DEBUG: historical message logs suppressed to reduce noise
                # for i, msg in enumerate(agent.messages):
                #     print(f"🔍 DEBUG: Historical message {i}: role={msg.get('role')}, content_type={type(msg.get('content'))}")
                #     content = msg.get('content', [])
                #     if isinstance(content, list) and len(content) > 0:
                #         first_block = content[0]
                #         if isinstance(first_block, dict) and 'text' in first_block:
                #             text_content = first_block['text']
                #             print(f"🔍 DEBUG: Content preview: {text_content[:100]}...")
                #         else:
                #             print(f"🔍 DEBUG: Content block: {first_block}")
                #     else:
                #         print(f"🔍 DEBUG: Content (unexpected format): {content}")

                # DEBUG: Current user prompt log suppressed

            print(f"✅ Agent created with {len(tools)} tools")

            # 使用异步流式调用替代同步调用
            print("🔍 DEBUG: Calling agent with async streaming...")

            try:
                # 使用异步流式调用
                response_parts = []
                tool_results = []  # 收集工具调用结果
                async for event in agent.stream_async(user_prompt):

                    # 处理流式事件并发送到前端
                    await handle_agent_event(event, session_id)

                    # 收集响应内容用于保存到数据库
                    if isinstance(event, dict) and 'event' in event and 'contentBlockDelta' in event['event']:
                        delta = event['event']['contentBlockDelta']['delta']
                        if 'text' in delta:
                            response_parts.append(delta['text'])

                    # 收集工具调用结果
                    if isinstance(event, dict) and 'toolResult' in event:
                        tool_result = event['toolResult']
                        if 'content' in tool_result:
                            for content in tool_result['content']:
                                if content.get('type') == 'text' and 'text' in content:
                                    tool_results.append(content['text'])
                                    print(f"🔍 DEBUG: Collected tool result: {content['text'][:100]}...")

                                    # 工具已经直接保存了图像/视频消息，这里不需要额外处理

                        # 兼容另一种事件形态：toolResult 位于顶层 message.content[*].toolResult
                        if isinstance(event, dict) and isinstance(event.get('message'), dict):
                            msg = event['message']
                            contents = msg.get('content') if isinstance(msg.get('content'), list) else []
                            for item in contents:
                                if isinstance(item, dict) and 'toolResult' in item:
                                    tr = item['toolResult']
                                    if isinstance(tr, dict) and 'content' in tr:
                                        for c in tr['content']:
                                            if c.get('type') == 'text' and 'text' in c:
                                                tool_results.append(c['text'])
                                                print(f"🔍 DEBUG: Collected tool result (message envelope): {c['text'][:100]}...")


                # 保存完整的文本消息到数据库（包括工具结果）
                all_content = response_parts + tool_results
                response_text = ''.join(all_content)
                if response_text.strip():  # 只保存非空消息
                    text_message = {
                        'role': 'assistant',
                        'content': response_text
                    }
                    db_service.create_message(session_id, 'assistant', json.dumps(text_message))
                    print(f"🔍 DEBUG: Saved message with {len(response_parts)} text parts and {len(tool_results)} tool results")

            except Exception as e:
                print(f"❌ Agent error: {e}")
                await send_user_websocket_message(session_id, {
                    'type': 'error',
                    'error': str(e)
                })

        # 发送完成事件
        await send_user_websocket_message(session_id, {
            'type': 'done'
        })

    except Exception as e:
        print('Error in strands_agent', e)
        traceback.print_exc()
        await send_user_websocket_message(session_id, {
            'type': 'error',
            'error': str(e)
        })


async def strands_multi_agent(messages, canvas_id, session_id, text_model, image_model, video_model=None, system_prompt: str = None):
    """多Agent Swarm处理"""
    try:
        model = create_model_instance(text_model)


        # 创建主Agent，使用专门化agent工具
        orchestrator_system_prompt = system_prompt or """
You are an intelligent orchestrator agent that coordinates multiple specialized agents to handle complex tasks.

Available Specialized Agents:
- planner_agent: Creates detailed execution plans and project breakdowns
- image_designer_agent: Generates images and handles visual content creation

Your Coordination Capabilities:
- Analyze complex projects and break them down into manageable components
- Coordinate multiple specialists working together on complex projects
- Manage task dependencies, sequencing, and resource allocation
- Track progress and ensure quality across multi-step workflows
- Provide comprehensive project management and execution guidance

Routing Guidelines:
1. For planning tasks → use planner_agent
2. For image/visual content → use image_designer_agent
3. For complex projects → coordinate specialists directly using your built-in capabilities

Always analyze the user's request and route to the most appropriate specialist(s).
You can use multiple agents in sequence for complex tasks and coordinate their work directly.
For analysis, research, or data processing tasks, use your own reasoning capabilities or route to planner_agent for structured analysis.
"""

        # 创建专门化agents作为工具
        specialized_agents = get_specialized_agents()

        print(f"🔧 Creating multi-agent with {len(specialized_agents)} specialized agents:")
        for i, agent_tool in enumerate(specialized_agents):
            agent_name = getattr(agent_tool, '__name__', str(agent_tool))
            agent_type = type(agent_tool).__name__
            print(f"  {i+1}. {agent_name} ({agent_type})")

        agent = Agent(
            model=model,
            tools=specialized_agents,
            system_prompt=orchestrator_system_prompt
        )

        # 设置历史消息到多Agent中，让模型能够访问完整的对话上下文
        if len(all_messages) > 1:  # 如果有历史消息
            # 将历史消息转换为Strands Agent期望的格式
            agent.messages = all_messages[:-1]  # 除了最后一条用户消息，其他都作为历史
            print(f"🔍 DEBUG: Multi-agent set {len(agent.messages)} historical messages to agent")

            # DEBUG: multi-agent historical message logs suppressed to reduce noise
            # for i, msg in enumerate(agent.messages):
            #     print(f"🔍 DEBUG: Multi-agent historical message {i}: role={msg.get('role')}, content_type={type(msg.get('content'))}")
            #     content = msg.get('content', [])
            #     if isinstance(content, list) and len(content) > 0:
            #         first_block = content[0]
            #         if isinstance(first_block, dict) and 'text' in first_block:
            #             text_content = first_block['text']
            #             print(f"🔍 DEBUG: Multi-agent content preview: {text_content[:100]}...")
            #         else:
            #             print(f"🔍 DEBUG: Multi-agent content block: {first_block}")
            #     else:
            #         print(f"🔍 DEBUG: Multi-agent content (unexpected format): {content}")

            # DEBUG: Multi-agent current user prompt log suppressed

        print(f"✅ Multi-agent created successfully")

        # 获取历史消息并转换为Strands格式（与单Agent模式相同的逻辑）
        try:
            # 获取数据库中的历史消息
            historical_messages = db_service.get_chat_history(session_id)
            print(f"🔍 DEBUG: Multi-agent retrieved {len(historical_messages)} historical messages from database")

            # 将当前传入的messages与历史消息合并，去重
            all_messages = []

            # 添加历史消息，过滤掉图像内容
            for hist_msg in historical_messages:
                if hist_msg.get('role') and hist_msg.get('content'):
                    # 处理content，过滤掉图像消息或转换为文本描述
                    content = hist_msg['content']
                    processed_content = _process_message_content_for_agent(content)

                    if processed_content:  # 只添加有效的文本内容
                        # 转换为Bedrock标准消息格式
                        all_messages.append({
                            'role': hist_msg['role'],
                            'content': [
                                {
                                    'text': processed_content
                                }
                            ]
                        })

            # 添加当前消息（如果不在历史中）
            for current_msg in messages:
                if current_msg.get('role') and current_msg.get('content'):
                    # 简单去重：检查最后几条消息是否已存在
                    is_duplicate = False
                    for existing_msg in all_messages[-3:]:  # 只检查最后3条避免性能问题
                        if (existing_msg.get('role') == current_msg.get('role') and
                            existing_msg.get('content') == current_msg.get('content')):
                            is_duplicate = True
                            break

                    if not is_duplicate:
                        # 转换当前消息为Bedrock标准格式
                        content = current_msg['content']
                        if isinstance(content, str):
                            formatted_content = [{'text': content}]
                        elif isinstance(content, list):
                            # 转换列表格式，确保使用Bedrock格式
                            formatted_content = []
                            for item in content:
                                if isinstance(item, dict):
                                    if item.get('type') == 'text' and 'text' in item:
                                        formatted_content.append({'text': item['text']})
                                    elif 'text' in item and 'type' not in item:
                                        formatted_content.append({'text': item['text']})
                                    elif isinstance(item.get('text'), str):
                                        formatted_content.append({'text': item['text']})
                                elif isinstance(item, str):
                                    formatted_content.append({'text': item})
                            if not formatted_content:
                                formatted_content = [{'text': str(content)}]
                        else:
                            formatted_content = [{'text': str(content)}]

                        all_messages.append({
                            'role': current_msg['role'],
                            'content': formatted_content
                        })

            print(f"🔍 DEBUG: Multi-agent total messages: {len(all_messages)}")

            # 获取最后一条用户消息作为当前prompt
            user_prompt = ""
            for msg in reversed(all_messages):
                if msg.get('role') == 'user':
                    user_prompt = msg.get('content', '')
                    break

            if not user_prompt:
                user_prompt = "Hello, how can I help you?"

        except Exception as e:
            print(f"❌ Multi-agent error retrieving chat history: {e}")
            # 降级处理：只使用当前传入的消息
            all_messages = []
            for msg in messages:
                if msg.get('role') and msg.get('content'):
                    # 转换为Bedrock标准格式
                    content = msg['content']
                    if isinstance(content, str):
                        formatted_content = [{'text': content}]
                    elif isinstance(content, list):
                        formatted_content = content  # 假设已经是正确格式
                    else:
                        formatted_content = [{'text': str(content)}]

                    all_messages.append({
                        'role': msg['role'],
                        'content': formatted_content
                    })

            user_prompt = ""
            for msg in reversed(messages):
                if msg.get('role') == 'user':
                    user_prompt = msg.get('content', '')
                    break

            if not user_prompt:
                user_prompt = "Hello, how can I help you?"

        # 使用上下文管理器设置会话上下文，传递当前用户ID


        try:
            current_user_id = get_current_user_id()
        except Exception:
            current_user_id = None

        # 准备模型上下文
        model_context = {'image': image_model}
        if video_model:
            model_context['video'] = video_model

        with SessionContextManager(session_id, canvas_id, model_context, user_id=current_user_id):
            print(f"🔍 DEBUG: Starting multi-agent stream call with prompt: {user_prompt}")
            print(f"🔍 DEBUG: Session context - session_id: {session_id}, canvas_id: {canvas_id}")
            print(f"🔍 DEBUG: Image model: {image_model}")

            # 使用异步流式调用替代同步调用
            print("🔍 DEBUG: Calling multi-agent with async streaming...")

            try:
                # 使用异步流式调用
                response_parts = []
                tool_results = []  # 收集工具调用结果
                async for event in agent.stream_async(user_prompt):
                    # 处理流式事件并发送到前端
                    await handle_agent_event(event, session_id)

                    # 收集响应内容用于保存到数据库
                    if isinstance(event, dict) and 'event' in event and 'contentBlockDelta' in event['event']:
                        delta = event['event']['contentBlockDelta']['delta']
                        if 'text' in delta:
                            response_parts.append(delta['text'])


                    # 收集工具调用结果
                    if isinstance(event, dict) and 'toolResult' in event:
                        tool_result = event['toolResult']
                        if 'content' in tool_result:

                            for content in tool_result['content']:
                                if content.get('type') == 'text' and 'text' in content:
                                    tool_results.append(content['text'])
                                    print(f"🔍 DEBUG: Multi-agent collected tool result: {content['text'][:100]}...")

                                    # 工具已经直接保存了图像/视频消息，这里不需要额外处理


                        # 兼容：toolResult 位于顶层 message.content[*].toolResult（multi-agent）
                        if isinstance(event, dict) and isinstance(event.get('message'), dict):
                            msg = event['message']
                            contents = msg.get('content') if isinstance(msg.get('content'), list) else []
                            for item in contents:
                                if isinstance(item, dict) and 'toolResult' in item:
                                    tr = item['toolResult']
                                    if isinstance(tr, dict) and 'content' in tr:
                                        for c in tr['content']:
                                            if c.get('type') == 'text' and 'text' in c:
                                                tool_results.append(c['text'])
                                                print(f"🔍 DEBUG: Multi-agent collected tool result (message envelope): {c['text'][:100]}...")

                # 保存完整的文本消息到数据库（包括工具结果）
                all_content = response_parts + tool_results
                response_text = ''.join(all_content)
                if response_text.strip():  # 只保存非空消息
                    text_message = {
                        'role': 'assistant',
                        'content': response_text
                    }
                    db_service.create_message(session_id, 'assistant', json.dumps(text_message))
                    print(f"🔍 DEBUG: Multi-agent saved message with {len(response_parts)} text parts and {len(tool_results)} tool results")

            except Exception as e:
                print(f"❌ Multi-agent error: {e}")
                await send_user_websocket_message(session_id, {
                    'type': 'error',
                    'error': str(e)
                })

        # 发送完成事件
        await send_user_websocket_message(session_id, {
            'type': 'done'
        })

    except Exception as e:
        print('Error in strands_multi_agent', e)
        tb_str = traceback.format_exc()
        print(f"Full traceback:\n{tb_str}")
        traceback.print_exc()
        await send_user_websocket_message(session_id, {
            'type': 'error',
            'error': str(e)
        })


async def handle_agent_event(event, session_id):
    """处理 Agent 事件"""
    if not isinstance(event, dict):
        return



    # 只处理重要的事件，减少噪音
    # 有些 SDK 版本会把当前工具调用信息放在顶层（不在 inner_event 里）
    if isinstance(event.get('current_tool_use'), dict):
        cur = event['current_tool_use']
        tool_use_id = cur.get('toolUseId', '')
        tool_name = cur.get('name', '')
        if tool_use_id:
            # 记录映射和当前会话的工具ID，便于后续 argument/stop 日志对齐
            _tool_call_names[tool_use_id] = tool_name or _tool_call_names.get(tool_use_id, '')
            prev = _current_tool_use_by_session.get(session_id)
            if prev != tool_use_id:
                _current_tool_use_by_session[session_id] = tool_use_id
                # 去重后打印“开始”日志（若之前未由 contentBlockStart 触发）
                event_key = f"tool_call_{session_id}_{tool_use_id}"
                if event_key not in _sent_events:
                    _sent_events.add(event_key)
                    print(f"🔧 Tool call started: {tool_name} (ID: {tool_use_id}) [from current_tool_use]")
                    await send_user_websocket_message(session_id, {
                        'type': 'tool_call',
                        'id': tool_use_id,
                        'name': tool_name,
                        'arguments': ''
                    })

    if 'event' in event:
        inner_event = event['event']

        # 处理工具调用开始
        if 'contentBlockStart' in inner_event:
            start = inner_event['contentBlockStart']['start']
            if 'toolUse' in start:
                tool_use = start['toolUse']
                tool_call_id = tool_use.get('toolUseId', '')

                # 检查是否已经发送过这个tool_call事件
                event_key = f"tool_call_{session_id}_{tool_call_id}"
                if event_key in _sent_events:
                    print(f"🔄 Skipping duplicate tool_call event: {tool_call_id}")
                    return

                # 记录 toolUseId 到工具名的映射，用于后续结果日志
                tool_name = tool_use.get('name', '')
                _tool_call_names[tool_call_id] = tool_name

                _sent_events.add(event_key)
                print(f"🔧 Tool call started: {tool_name} (ID: {tool_call_id})")
                await send_user_websocket_message(session_id, {
                    'type': 'tool_call',
                    'id': tool_call_id,
                    'name': tool_name,
                    'arguments': ''
                })

        # 某些实现会在这里给出 messageStop: tool_use，表示模型已发出工具调用并暂停等待
        elif 'messageStop' in inner_event:
            stop = inner_event['messageStop'] or {}
            reason = stop.get('stopReason') or stop.get('reason')
            if reason == 'tool_use':
                tool_use_id = _current_tool_use_by_session.get(session_id, '')
                tool_name = _tool_call_names.get(tool_use_id, '')
                print(f"🔧 Message stopped for tool_use → pending execution. Current tool: {tool_name} (ID: {tool_use_id})")


        # 处理文本和工具参数增量
        elif 'contentBlockDelta' in inner_event:
            delta = inner_event['contentBlockDelta']['delta']
            if 'text' in delta:
                await send_user_websocket_message(session_id, {
                    'type': 'delta',
                    'text': delta['text']
                })
            elif 'toolUse' in delta:
                await send_user_websocket_message(session_id, {
                    'type': 'tool_call_arguments',
                    'id': _current_tool_use_by_session.get(session_id, ''),
                    'text': delta['toolUse'].get('input', '')
                })

        # 处理工具调用完成
        elif 'contentBlockStop' in inner_event:
            stop_info = inner_event['contentBlockStop']
            if 'toolUse' in stop_info:
                tool_use = stop_info.get('toolUse', {})
                tool_use_id = tool_use.get('toolUseId', '')
                tool_name = _tool_call_names.get(tool_use_id, tool_use.get('name', ''))
                print(f"🔧 Tool call completed: {tool_use_id} ({tool_name})")

    # 处理工具调用结果
    if 'toolResult' in event:
        tool_result = event['toolResult']
        tool_use_id = tool_result.get('toolUseId', 'unknown')

        tool_name = _tool_call_names.get(tool_use_id, '')
        print(f"🔧 Tool result received: {tool_use_id} ({tool_name})")

        # 发送工具结果到前端（如果需要），并增强日志（尤其是 use_agent）
        if 'content' in tool_result:
            for content in tool_result['content']:
                if content.get('type') == 'text' and 'text' in content:
                    text = content['text']
                    # 特殊处理 use_agent：打印完整返回并尝试解析 JSON
                    if 'use_agent' in (tool_name or ''):
                        print(f"🧭 use_agent raw result: {text}")
                        try:
                            parsed = json.loads(text)
                            mode = parsed.get('mode')
                            generation_model = parsed.get('generation_model')
                            reasoning = parsed.get('reasoning')
                            print(f"🧭 use_agent parsed → mode={mode}, generation_model={generation_model}, reasoning={reasoning}")
                            # Store into session context for tool fallback consumption
                            try:
                                set_intention_result(parsed)
                            except Exception as _se:
                                print(f"⚠️ Failed to store intention result in session context: {_se}")
                        except Exception:
                            # 非严格 JSON 时忽略解析失败
                            pass
                    else:
                        # 其他工具的结果做简略日志
                        print(f"🔍 DEBUG: Tool ({tool_name}) result: {text[:200]}...")

                    # 向前端发送工具结果作为 delta
                    await send_user_websocket_message(session_id, {
                        'type': 'delta',
                        'text': text
                    })

    # 注释掉重复的文本处理逻辑，避免重复发送delta事件

    # 兼容：toolResult 位于顶层 message.content[*].toolResult（与上面的顶层 toolResult 平行）
    if isinstance(event.get('message'), dict):
        msg = event['message']
        contents = msg.get('content') if isinstance(msg.get('content'), list) else []
        for item in contents:
            if isinstance(item, dict) and 'toolResult' in item:
                tool_result = item['toolResult']
                if not isinstance(tool_result, dict):
                    continue
                tool_use_id = tool_result.get('toolUseId', 'unknown')
                tool_name = _tool_call_names.get(tool_use_id, '')
                print(f"🔧 Tool result received: {tool_use_id} ({tool_name}) [message envelope]")

                if 'content' in tool_result:
                    for content in tool_result['content']:
                        if content.get('type') == 'text' and 'text' in content:
                            text = content['text']
                            if 'use_agent' in (tool_name or ''):
                                print(f"🧭 use_agent raw result: {text}")
                                try:
                                    parsed = json.loads(text)
                                    mode = parsed.get('mode')
                                    generation_model = parsed.get('generation_model')
                                    reasoning = parsed.get('reasoning')
                                    print(f"🧭 use_agent parsed → mode={mode}, generation_model={generation_model}, reasoning={reasoning}")
                                    # Store into session context for tool fallback consumption
                                    try:
                                        set_intention_result(parsed)
                                    except Exception as _se:
                                        print(f"⚠️ Failed to store intention result in session context: {_se}")
                                except Exception:
                                    pass
                            else:
                                print(f"🔍 DEBUG: Tool ({tool_name}) result: {text[:200]}...")

                            await send_user_websocket_message(session_id, {
                                'type': 'delta',
                                'text': text
                            })

    # elif "data" in event and "delta" in event:
    #     # 处理包含文本的数据事件，但避免重复处理已经在上面处理过的事件
    #     if isinstance(event.get("data"), str) and event["data"].strip():
    #         # 这是一个包含文本的数据事件
    #         await send_user_websocket_message(session_id, {
    #             'type': 'delta',
    #             'text': event["data"]
    #         })


# 向后兼容的别名
clean_strands_agent = strands_agent
handle_clean_agent_event = handle_agent_event


# 支持并行agent的辅助函数
def create_parallel_agents(agent_type: str, count: int, base_config: dict) -> list:
    """创建并行agent配置"""
    parallel_agents = []
    for i in range(count):
        agent_config = base_config.copy()
        agent_config['name'] = f"{agent_type}_{i+1}"
        parallel_agents.append(agent_config)
    return parallel_agents
