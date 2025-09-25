"""
Strands格式的图像生成工具
只包含带上下文的图像生成工具
注意：此文件提供工具创建函数，不直接注册为工具
"""

# 告诉 strands 库不要自动注册此文件为工具
# 这可以防止 "tool function missing" 警告
__STRANDS_TOOL__ = False
__all__ = ['create_generate_image_with_context', 'generate_file_id', 'generate_image_id', 'strands_image_generators']

# 全局变量来跟踪已发送的file_generated事件，防止重复
_sent_file_events = set()
import random
import base64
import json
import traceback
import os
import asyncio
from mimetypes import guess_type
from typing import List, Dict, Optional, Annotated

from pydantic import BaseModel, Field
from strands import tool
import aiofiles
try:
    from nanoid import generate
except ImportError:
    # Fallback ID generation if nanoid is not available
    import random
    import string
    def generate(size=8):
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=size))

from common import DEFAULT_PORT
from services.config_service import FILES_DIR
from services.db_service import db_service
from services.websocket_service import send_to_websocket, broadcast_session_update

# run_async_safe 函数已移除 - 现在直接使用 async/await

# Import all generators with absolute imports
try:
    # 尝试绝对导入
    import sys
    import os

    # 确保服务器根目录在 Python 路径中
    server_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if server_root not in sys.path:
        sys.path.insert(0, server_root)

    from tools.img_generators import (
        ReplicateGenerator,
        ComfyUIGenerator,
        WavespeedGenerator,
        JaazGenerator,
        OpenAIGenerator
    )
    print("✅ Image generators imported successfully")

except ImportError as e:
    print(f"❌ Failed to import image generators: {e}")
    # 创建空的生成器类作为备用
    class DummyGenerator:
        async def generate(self, *args, **kwargs):
            raise NotImplementedError("Image generator not available")

    ReplicateGenerator = DummyGenerator
    ComfyUIGenerator = DummyGenerator
    WavespeedGenerator = DummyGenerator
    JaazGenerator = DummyGenerator
    OpenAIGenerator = DummyGenerator


# 生成唯一文件 ID
def generate_file_id():
    return 'im_' + generate(size=8)


def get_recent_images_from_session(session_id: str, user_id: str = None, count: int = 5) -> List[Dict]:
    """
    从指定session中获取最近的N张图像（包括用户上传的和助手生成的），按时间排序

    Args:
        session_id: 会话ID
        user_id: 用户ID，如果提供则用于用户验证
        count: 获取图像数量，默认5张

    Returns:
        List[Dict]: [
            {
                "file_id": "im_xxx.png",
                "timestamp": "2024-01-01T12:00:00Z",
                "index": 1  # 第几张图像（从1开始）
            }
        ]
    """
    try:
        # print(f"🔍 DEBUG: get_recent_images_from_session called with session_id={session_id}, user_id={user_id}, count={count}")
        # 获取session的聊天历史
        if user_id:
            from services.user_context import UserContextManager
            with UserContextManager(user_id):
                messages = db_service.get_chat_history(session_id)
        else:
            try:
                messages = db_service.get_chat_history(session_id)
            except Exception as auth_error:
                try:
                    from services.strands_context import get_user_id
                    strands_user_id = get_user_id()
                    if strands_user_id:
                        from services.user_context import UserContextManager
                        with UserContextManager(strands_user_id):
                            messages = db_service.get_chat_history(session_id)
                    else:
                        raise auth_error
                except Exception as e:
                    raise auth_error

        # 收集所有图像，按时间排序
        images = []
        for message in messages:
            if not isinstance(message, dict):
                continue

            # 获取消息时间戳
            timestamp = message.get('timestamp', message.get('created_at', ''))

            if message.get('content'):
                content = message.get('content', [])

                # 处理字符串格式的content
                if isinstance(content, str):
                    try:
                        import json
                        parsed_content = json.loads(content)
                        if isinstance(parsed_content, list):
                            for item in parsed_content:
                                if (isinstance(item, dict) and
                                    item.get('type') == 'image_url' and
                                    item.get('image_url', {}).get('url')):
                                    url = item['image_url']['url']
                                    if '/api/file/' in url:
                                        file_id = url.split('/api/file/')[-1]
                                        images.append({
                                            'file_id': file_id,
                                            'timestamp': timestamp,
                                            'message_role': message.get('role', 'unknown')
                                        })
                    except (json.JSONDecodeError, TypeError):
                        pass

                    # 查找字符串中的图像引用
                    import re
                    image_pattern = r'/api/file/(im_[a-zA-Z0-9_-]+\.[a-zA-Z0-9]+)'
                    matches = re.findall(image_pattern, content)
                    for file_id in matches:
                        images.append({
                            'file_id': file_id,
                            'timestamp': timestamp,
                            'message_role': message.get('role', 'unknown')
                        })

                # 处理列表格式的content
                elif isinstance(content, list):
                    for item in content:
                        if (isinstance(item, dict) and
                            item.get('type') == 'image_url' and
                            item.get('image_url', {}).get('url')):
                            url = item['image_url']['url']
                            if '/api/file/' in url:
                                file_id = url.split('/api/file/')[-1]
                                images.append({
                                    'file_id': file_id,
                                    'timestamp': timestamp,
                                    'message_role': message.get('role', 'unknown')
                                })

        # 按时间戳排序（最新的在前）
        images.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

        # 去重（保留最新的）
        seen_files = set()
        unique_images = []
        for img in images:
            if img['file_id'] not in seen_files:
                seen_files.add(img['file_id'])
                unique_images.append(img)

        # 限制数量并添加索引
        result = []
        for i, img in enumerate(unique_images[:count]):
            result.append({
                'file_id': img['file_id'],
                'timestamp': img['timestamp'],
                'index': i + 1,
                'message_role': img['message_role']
            })

        # print(f"🎯 Found {len(result)} recent images in session")
        return result

    except Exception as e:
        print(f"❌ Error getting recent images from session {session_id}: {e}")
        return []


def get_most_recent_image_from_session(session_id: str, user_id: str = None) -> str:
    """
    从指定session中获取最近的图像ID（包括用户上传的和助手生成的）

    Args:
        session_id: 会话ID
        user_id: 用户ID，如果提供则用于用户验证

    Returns:
        最近图像的文件ID，如果没有找到则返回空字符串
    """
    try:
        print(f"🔍 DEBUG: get_most_recent_image_from_session called with session_id={session_id}, user_id={user_id}")
        # 获取session的聊天历史
        if user_id:
            # 如果提供了user_id，直接使用user-aware服务
            print(f"🔍 DEBUG: Using UserContextManager with user_id={user_id}")
            from services.user_context import UserContextManager
            with UserContextManager(user_id):
                messages = db_service.get_chat_history(session_id)
                print(f"🔍 DEBUG: Successfully got {len(messages)} messages from chat history")
        else:
            print(f"🔍 DEBUG: No user_id provided, trying current context")
            # 尝试从当前上下文获取user_id
            try:
                messages = db_service.get_chat_history(session_id)
                print(f"🔍 DEBUG: Successfully got {len(messages)} messages from current context")
            except Exception as auth_error:
                print(f"🔍 DEBUG: Auth error in current context: {auth_error}")
                # 如果认证失败，尝试从strands上下文获取user_id
                try:
                    from services.strands_context import get_user_id
                    strands_user_id = get_user_id()
                    print(f"🔍 DEBUG: Got strands_user_id: {strands_user_id}")
                    if strands_user_id:
                        from services.user_context import UserContextManager
                        with UserContextManager(strands_user_id):
                            messages = db_service.get_chat_history(session_id)
                            print(f"🔍 DEBUG: Successfully got {len(messages)} messages with strands user context")
                    else:
                        raise auth_error
                except Exception as e:
                    print(f"🔍 DEBUG: Failed to get user from strands context: {e}")
                    raise auth_error

        # 从最新的消息开始查找图像消息
        for i, message in enumerate(reversed(messages)):
            # 确保message是字典类型
            if not isinstance(message, dict):
                continue
            # 查找助手生成的图像和用户上传的图像
            if message.get('content'):
                content = message.get('content', [])

                # 处理字符串格式的content（可能是JSON字符串或包含图像引用的文本）
                if isinstance(content, str):
                    # 首先尝试解析为JSON
                    try:
                        import json
                        parsed_content = json.loads(content)
                        if isinstance(parsed_content, list):
                            # 递归处理解析后的列表
                            for item in parsed_content:
                                if (isinstance(item, dict) and
                                    item.get('type') == 'image_url' and
                                    item.get('image_url', {}).get('url')):

                                    url = item['image_url']['url']
                                    if '/api/file/' in url:
                                        file_id = url.split('/api/file/')[-1]
                                        print(f"🎯 Found recent image in session (from JSON): {file_id}")
                                        return file_id
                        elif isinstance(parsed_content, dict) and parsed_content.get('content'):
                            # 处理嵌套的content结构
                            nested_content = parsed_content['content']
                            if isinstance(nested_content, list):
                                for item in nested_content:
                                    if (isinstance(item, dict) and
                                        item.get('type') == 'image_url' and
                                        item.get('image_url', {}).get('url')):

                                        url = item['image_url']['url']
                                        if '/api/file/' in url:
                                            file_id = url.split('/api/file/')[-1]
                                            print(f"🎯 Found recent image in session (from nested JSON): {file_id}")
                                            return file_id
                    except (json.JSONDecodeError, TypeError):
                        # 如果不是JSON，则作为普通字符串处理
                        pass

                    # 查找字符串中的图像引用，如 ![...](/api/file/im_xxx.jpeg)
                    import re
                    image_pattern = r'/api/file/(im_[a-zA-Z0-9_-]+\.[a-zA-Z0-9]+)'
                    matches = re.findall(image_pattern, content)
                    if matches:
                        file_id = matches[-1]  # 取最后一个匹配的图像
                        print(f"🎯 Found recent image in session (from text): {file_id}")
                        return file_id

                # 处理列表格式的content
                elif isinstance(content, list):
                    for item in content:
                        if (isinstance(item, dict) and
                            item.get('type') == 'image_url' and
                            item.get('image_url', {}).get('url')):

                            url = item['image_url']['url']
                            # 从URL中提取文件ID，例如 '/api/file/im_abc123.png' -> 'im_abc123.png'
                            if '/api/file/' in url:
                                file_id = url.split('/api/file/')[-1]
                                print(f"🎯 Found recent image in session (from list): {file_id}")
                                return file_id

        return ""

    except Exception as e:
        print(f"❌ Error getting recent image from session {session_id}: {e}")
        return ""


def parse_image_references_with_llm(prompt: str, available_images: List[Dict], model_config: dict = None) -> Dict:
    """
    使用Strands服务中的analyze_image_intent工具进行LLM分析解析用户prompt中的图像引用
    注意：这个函数现在主要作为向后兼容的接口，实际的LLM分析应该通过Agent调用analyze_image_intent工具
    """
    try:
        print(f"🔍 DEBUG: 使用LLM分析图像意图，prompt: {prompt}, 可用图像数: {len(available_images)}")

        # 这里暂时回退到关键词匹配，因为真正的LLM分析应该在Agent层面进行
        # 当Agent有analyze_image_intent工具时，它会自动调用该工具进行分析
        print(f"🔍 DEBUG: 当前在工具内部，回退到关键词匹配")
        return parse_image_references_with_keywords(prompt, available_images)

    except Exception as e:
        print(f"❌ LLM分析失败，回退到关键词匹配: {e}")
        return parse_image_references_with_keywords(prompt, available_images)


def parse_image_references_with_keywords(prompt: str, available_images: List[Dict]) -> Dict:
    """
    使用关键词匹配解析用户prompt中的图像引用（备用方法）
    """
    print(f"🔍 DEBUG: 关键词分析用户prompt: {prompt}")
    print(f"🔍 DEBUG: 可用图像数量: {len(available_images)}")

    # 使用关键词匹配和语义分析
    referenced_images = []
    fusion_mode = 'auto'

    # 检测图像引用的关键词模式
    image_reference_patterns = [
        # 中文数字表达
        ('第一张', 1), ('第二张', 2), ('第三张', 3),
        ('第一个', 1), ('第二个', 2), ('第三个', 3),
        ('第一幅', 1), ('第二幅', 2), ('第三幅', 3),
        # 阿拉伯数字表达
        ('图1', 1), ('图2', 2), ('图3', 3),
        ('图片1', 1), ('图片2', 2), ('图片3', 3),
        ('第1张', 1), ('第2张', 2), ('第3张', 3),
        # 英文表达
        ('first image', 1), ('second image', 2), ('third image', 3),
        ('image 1', 1), ('image 2', 2), ('image 3', 3),
    ]

    # 隐式单图像引用关键词（指向最近的图像）
    implicit_single_image_keywords = [
        '这张图', '这个图', '这幅图', '图片', '图像', '照片',
        'this image', 'the image', 'this picture', 'the picture', 'this photo',
        '修改', '编辑', '改变', '调整', '优化', '美化',
        'modify', 'edit', 'change', 'adjust', 'enhance', 'improve',
        # 添加更多单图编辑相关的关键词
        '背景', '换成', '替换', '变成', '改成', '颜色', '风格',
        'background', 'replace', 'change to', 'make it', 'turn into'
    ]

    # 检查prompt中的明确图像引用
    prompt_lower = prompt.lower()
    for pattern, index in image_reference_patterns:
        if pattern in prompt or pattern in prompt_lower:
            if index <= len(available_images):
                referenced_images.append(index)
                print(f"🎯 检测到明确图像引用: '{pattern}' -> 图像{index}")

    # 如果没有明确引用，检查隐式单图像引用
    if not referenced_images:
        print(f"🔍 DEBUG: 检查隐式单图像引用关键词...")
        matched_keywords = []
        for keyword in implicit_single_image_keywords:
            if keyword in prompt or keyword in prompt_lower:
                matched_keywords.append(keyword)

        print(f"🔍 DEBUG: 匹配到的隐式关键词: {matched_keywords}")
        has_implicit_reference = len(matched_keywords) > 0

        if has_implicit_reference and len(available_images) >= 1:
            referenced_images = [1]  # 使用最近的图像
            print(f"🎯 检测到隐式图像引用，使用最近的图像，匹配关键词: {matched_keywords}")
        else:
            print(f"🔍 DEBUG: 没有检测到隐式图像引用")

    # 去重并排序
    referenced_images = sorted(list(set(referenced_images)))

    # 检测融合意图的关键词
    blend_keywords = [
        '融合', '合并', '结合', '混合', '组合', '叠加',
        'blend', 'mix', 'combine', 'merge', 'fusion', 'overlay'
    ]

    style_transfer_keywords = [
        '风格', '样式', '戴上', '带上', '穿上', '应用',
        'style', 'transfer', 'apply', 'wear', 'put on'
    ]

    # 判断融合模式
    print(f"🔍 DEBUG: 检查融合意图关键词...")
    matched_blend_keywords = []
    for keyword in blend_keywords:
        if keyword in prompt_lower:
            matched_blend_keywords.append(keyword)

    matched_style_keywords = []
    for keyword in style_transfer_keywords:
        if keyword in prompt_lower:
            matched_style_keywords.append(keyword)

    print(f"🔍 DEBUG: 匹配到的融合关键词: {matched_blend_keywords}")
    print(f"🔍 DEBUG: 匹配到的风格迁移关键词: {matched_style_keywords}")

    has_blend_intent = len(matched_blend_keywords) > 0
    has_style_intent = len(matched_style_keywords) > 0

    if has_style_intent:
        fusion_mode = 'style_transfer'
        print(f"🎯 检测到风格迁移意图，匹配关键词: {matched_style_keywords}")
        # 如果是风格迁移但没有明确引用，假设使用前两张图像
        if not referenced_images and len(available_images) >= 2:
            referenced_images = [1, 2]
            print(f"🎯 风格迁移模式，自动使用前两张图像")
    elif has_blend_intent:
        fusion_mode = 'blend'
        print(f"🎯 检测到图像融合意图，匹配关键词: {matched_blend_keywords}")
        # 如果是融合但没有明确引用，假设使用前两张图像
        if not referenced_images and len(available_images) >= 2:
            referenced_images = [1, 2]
            print(f"🎯 融合模式，自动使用前两张图像")
    else:
        print(f"🔍 DEBUG: 没有检测到融合或风格迁移意图")

    # 验证引用的图像是否存在
    valid_references = []
    for num in referenced_images:
        if 1 <= num <= len(available_images):
            valid_references.append(num)

    # 构建结果
    result = {
        'referenced_images': valid_references,
        'fusion_mode': fusion_mode,
        'processed_prompt': prompt,  # 保持原prompt
        'model_suggestion': 'flux-kontext'
    }

    # 建议模型
    if len(valid_references) >= 2:
        result['model_suggestion'] = 'flux-kontext-multiple'
        print(f"🎯 建议使用多图像模型: flux-kontext-multiple")
    elif len(valid_references) == 1:
        result['model_suggestion'] = 'flux-kontext'
        print(f"🎯 建议使用单图像模型: flux-kontext")
    else:
        result['model_suggestion'] = 'flux-t2i'
        print(f"🎯 建议使用文本到图像模型: flux-t2i")

    print(f"🎯 智能分析最终结果: 引用图像{valid_references}, 模式:{fusion_mode}, 模型:{result['model_suggestion']}")
    return result


def parse_image_references_regex(prompt: str, available_images: List[Dict]) -> Dict:
    """
    使用正则表达式解析用户prompt中的图像引用（备用方法）
    """
    import re

    result = {
        'referenced_images': [],
        'fusion_mode': 'auto',
        'processed_prompt': prompt,
        'model_suggestion': 'flux-kontext'
    }

    # 提取数字引用：第1张、第2张、图像1、image 1等
    number_patterns = [
        r'第(\d+)张',
        r'第(\d+)个',
        r'图像(\d+)',
        r'图片(\d+)',
        r'图(\d+)',  # 添加对 "图1", "图2" 的支持
        r'image\s*(\d+)',
        r'pic\s*(\d+)',
        r'(\d+)号图'
    ]

    # 中文数字映射
    chinese_numbers = {
        '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
        '六': 6, '七': 7, '八': 8, '九': 9, '十': 10
    }

    # 中文数字引用模式：第一张、第二张等（允许后面跟其他词汇）
    chinese_patterns = [
        r'第([一二三四五六七八九十])张',
        r'第([一二三四五六七八九十])个',
        r'第([一二三四五六七八九十])幅',
        r'第([一二三四五六七八九十])张[^，。]*',  # 第一张模特、第二张里面等
        r'第([一二三四五六七八九十])个[^，。]*',  # 第一个图像等
    ]

    referenced_numbers = []

    # 处理阿拉伯数字模式
    for pattern in number_patterns:
        matches = re.findall(pattern, prompt.lower())
        referenced_numbers.extend([int(m) for m in matches])

    # 处理中文数字模式
    for pattern in chinese_patterns:
        matches = re.findall(pattern, prompt)
        for match in matches:
            if match in chinese_numbers:
                referenced_numbers.append(chinese_numbers[match])

    # 去重并排序
    referenced_numbers = sorted(list(set(referenced_numbers)))

    # 检查特殊关键词
    multi_image_keywords = [
        '融合', '合并', '结合', '混合', '组合',
        'blend', 'mix', 'combine', 'merge', 'fusion',
        '两张', '多张', '前面', '所有'
    ]

    style_keywords = [
        '风格', '样式', 'style', 'transfer', '迁移'
    ]

    # 判断是否有多图像意图
    has_multi_intent = any(keyword in prompt.lower() for keyword in multi_image_keywords)
    has_style_intent = any(keyword in prompt.lower() for keyword in style_keywords)

    # 如果没有明确数字引用但有多图像关键词
    if not referenced_numbers and has_multi_intent:
        if '两张' in prompt or 'two' in prompt.lower():
            referenced_numbers = [1, 2]
        elif '前面' in prompt or 'previous' in prompt.lower():
            # 使用最近的2张图像
            referenced_numbers = [1, 2] if len(available_images) >= 2 else [1]
        elif '所有' in prompt or 'all' in prompt.lower():
            # 使用所有可用图像（最多3张）
            referenced_numbers = list(range(1, min(len(available_images) + 1, 4)))

    # 验证引用的图像是否存在
    valid_references = []
    for num in referenced_numbers:
        if 1 <= num <= len(available_images):
            valid_references.append(num)

    result['referenced_images'] = valid_references

    # 确定融合模式
    if has_style_intent:
        result['fusion_mode'] = 'style_transfer'
    elif has_multi_intent and len(valid_references) >= 2:
        result['fusion_mode'] = 'blend'
    else:
        result['fusion_mode'] = 'auto'

    # 建议模型
    if len(valid_references) >= 2:
        result['model_suggestion'] = 'flux-kontext-multiple'
    elif len(valid_references) == 1:
        result['model_suggestion'] = 'flux-kontext'
    else:
        result['model_suggestion'] = 'flux-t2i'

    # 处理prompt（移除图像引用词汇，保留核心描述）
    processed = prompt
    for pattern in number_patterns:
        processed = re.sub(pattern, '', processed, flags=re.IGNORECASE)

    # 清理多余的空格和标点
    processed = re.sub(r'\s+', ' ', processed).strip()
    processed = re.sub(r'^[,，、\s]+|[,，、\s]+$', '', processed)

    if not processed:
        processed = "create artistic image"

    result['processed_prompt'] = processed

    print(f"🔍 DEBUG: Image reference parsing result: {result}")
    return result


def select_optimal_model(prompt: str, available_images: List[Dict], current_model: str, model_config: dict = None) -> str:
    """
    根据prompt和可用图像智能选择最优模型
    """
    parse_result = parse_image_references_with_llm(prompt, available_images, model_config)
    suggested_model = parse_result['model_suggestion']

    # 如果当前模型已经合适，就不改变
    if current_model == suggested_model:
        return current_model

    # 如果建议使用多图像模型但当前模型不支持，则切换
    if suggested_model == 'flux-kontext-multiple':
        return 'flux-kontext-multiple'

    return current_model


# Initialize provider instances
PROVIDERS = {
    'replicate': ReplicateGenerator(),
    'comfyui': ComfyUIGenerator(),
    'wavespeed': WavespeedGenerator(),
    'jaaz': JaazGenerator(),
    'openai': OpenAIGenerator(),
}


def create_generate_image_with_context(session_id: str, canvas_id: str, image_model: dict, user_id: str = None):
    """创建一个带有上下文信息的 generate_image 工具"""
    from strands import tool

    @tool
    async def generate_image_with_context(
        prompt: Annotated[str, Field(description="Detailed description of the image to generate")],
        aspect_ratio: Annotated[str, Field(description="Aspect ratio for the image (1:1, 4:3, 16:9, 3:4)")] = "1:1",
        input_image: Annotated[str, Field(description="Optional image to use as reference. Pass image_id here, e.g. 'im_jurheut7.png'. Leave empty if not needed. Best for image editing cases like: Editing specific parts of the image, Removing specific objects, Maintaining visual elements across scenes")] = "",
        use_previous_image: Annotated[bool, Field(description="Whether to automatically use the most recent image from the current session as input. Set to TRUE when you want to edit, modify, or build upon the previously generated image (e.g., 'change the color', 'add something', 'remove object'). Set to FALSE when creating a completely new, unrelated image or when the user explicitly asks for a new image.")] = True,
        enable_multi_image: Annotated[bool, Field(description="Whether to enable automatic multi-image detection and fusion. Set to TRUE to allow the system to automatically detect when user wants to combine multiple images (e.g., 'blend first and second image'). Set to FALSE to disable multi-image features.")] = True,
        model_override: Annotated[str, Field(description="Override model to use (e.g., 'flux-kontext' or 'flux-kontext-multiple'). If set, this takes precedence over configured image model.")] = ""
    ) -> str:
        """
        Generate an image based on the provided prompt and parameters.

        This tool creates images using AI image generation models. It supports various aspect ratios
        and can optionally use an input image as reference for editing or style transfer.

        Args:
            prompt: Detailed description of what the image should contain
            aspect_ratio: The desired aspect ratio (1:1, 4:3, 16:9, 3:4)
            input_image: Optional reference image ID for editing tasks
            use_previous_image: Whether to automatically use the most recent image from the current session as input

        Returns:
            A message indicating successful image generation with file details
        """
        # 生成唯一的调用ID来跟踪重复调用
        call_id = generate_file_id()
        print(f"🎨️ generate_image_with_context tool called! Call ID: {call_id}")
        print(f"🔍 DEBUG: Using provided context - session_id: {session_id}, canvas_id: {canvas_id}")
        print(f"🔍 DEBUG: Using provided image_model: {image_model}")
        print(f"🔍 DEBUG: Using provided user_id: {user_id}")

        try:
            # 使用提供的上下文信息而不是从contextvars获取
            tool_call_id = generate_file_id()

            # Normalize model_override to avoid FieldInfo default leaking in when not passed
            print(f"🔍 DEBUG: Incoming model_override (raw) = {model_override} | type={type(model_override)}")
            _override = model_override if isinstance(model_override, str) else ""

            # Tool-layer fallback A: if no explicit override, read generation_model from session intention
            if not _override.strip():
                try:
                    from services.strands_context import get_intention_result as _get_intent
                    intent = _get_intent()
                    gm = (intent or {}).get('generation_model')
                    if isinstance(gm, str) and gm.strip():
                        _override = gm.strip()
                        print(f"🎯 Using session intention generation_model as override: {_override}")
                except Exception as _e:
                    print(f"⚠️ Failed to read intention result for fallback override: {_e}")

            model = (_override.strip()) or image_model.get('model', 'flux-kontext')
            if not isinstance(model, str):
                # Final guardrail
                model = str(model)

            # Handle empty provider - use default if provider is empty string
            provider = image_model.get('provider', 'comfyui')
            if not provider or provider.strip() == '':
                provider = 'comfyui'

            print(f"🔍 DEBUG: Selected model={model}, provider={provider}")
            print(f"🔍 DEBUG: enable_multi_image={enable_multi_image}")

            # Get provider instance
            generator = PROVIDERS.get(provider)
            if not generator:
                raise ValueError(f"Unsupported provider: {provider}")

            # Handle input_image parameter
            if not isinstance(input_image, str):
                input_image = ""

            # Multi-image detection and processing
            multi_image_context = None
            if enable_multi_image and not input_image:
                # Get recent images from session
                effective_user_id = user_id
                if not effective_user_id:
                    try:
                        from services.strands_context import get_user_id
                        effective_user_id = get_user_id()
                    except Exception:
                        pass

                # 简化逻辑：移除复杂的意图分析，让Agent在更高层面做决策
                print(f"🔍 DEBUG: Simplified image generation logic - Agent will handle intent analysis")

            # Check if the model supports input images before using previous image
            model_supports_input = 'kontext' in model.lower() or 'i2v' in model.lower() or 'edit' in model.lower()

            # Handle use_previous_image parameter - only for models that support input images
            if use_previous_image and not input_image and model_supports_input:
                print(f"� Using previous image from session")
                try:
                    # Use the user_id passed to the function
                    effective_user_id = user_id
                    if not effective_user_id:
                        # Fallback: try to get user_id from strands context if not provided
                        try:
                            from services.strands_context import get_user_id
                            effective_user_id = get_user_id()
                            print(f"🔍 DEBUG: Got user_id from strands context: {effective_user_id}")
                        except Exception as e:
                            print(f"🔍 DEBUG: Failed to get user_id from strands context: {e}")
                            pass

                    # Get the most recent image from the current session
                    print(f"🔍 DEBUG: Calling get_most_recent_image_from_session with session_id={session_id}, user_id={effective_user_id}")
                    previous_image_id = get_most_recent_image_from_session(session_id, effective_user_id)
                    if previous_image_id:
                        # Convert the file to base64
                        try:
                            # 首先尝试从数据库获取文件信息
                            file_record = None
                            file_id_without_ext = previous_image_id.split('.')[0] if '.' in previous_image_id else previous_image_id
                            try:
                                file_record = db_service.get_file(file_id_without_ext)
                            except Exception as db_error:
                                pass  # 静默处理数据库查找错误

                            file_path = None
                            if file_record:
                                # 使用数据库中的文件路径
                                file_path = os.path.join(FILES_DIR, file_record['file_path'])
                            else:
                                # 尝试直接路径
                                file_path = os.path.join(FILES_DIR, previous_image_id)

                                # 如果文件不存在且没有扩展名，尝试常见扩展名
                                if not os.path.exists(file_path) and '.' not in previous_image_id:
                                    for ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
                                        test_path = os.path.join(FILES_DIR, f'{previous_image_id}.{ext}')
                                        if os.path.exists(test_path):
                                            file_path = test_path
                                            break

                            if file_path and os.path.exists(file_path):
                                with open(file_path, 'rb') as f:
                                    image_data = f.read()
                                    input_image = base64.b64encode(image_data).decode('utf-8')
                                    print(f"✅ Previous image loaded successfully")
                            else:
                                print(f"❌ Previous image file not found")
                                return "I found a reference to a previous image in this conversation, but the image file is no longer available. Please upload a new image that I can help you edit."
                        except Exception as file_error:
                            print(f"❌ Error reading previous image file: {file_error}")
                            return "I found a previous image in this conversation, but I encountered an error while trying to access it. Please upload a new image that I can help you edit."
                    else:
                        # 当没有找到图像时，返回友好的错误信息
                        return "I don't see any previous images in this conversation that I can edit or modify. Please upload an image first, or create a new image that I can then help you edit."
                except Exception as e:
                    print(f"❌ Error getting previous image: {e}")
                    return "I encountered an error while trying to access previous images in this conversation. Please upload an image or try again."
            elif use_previous_image and not input_image and not model_supports_input:
                # User wants to use previous image but the model doesn't support it
                print(f"⚠️ Model {model} doesn't support input images, ignoring use_previous_image=True")
                # Continue with text-to-image generation without previous image

            print(f"🎨 Generating image: {model}")

            # Process input_image if provided
            processed_input_image = None
            if input_image and input_image.strip():

                # Check if it's already base64 (from use_previous_image processing)
                if use_previous_image and len(input_image) > 100 and not input_image.startswith('im_'):
                    # It's already base64 encoded from previous image processing
                    processed_input_image = input_image
                # Check if input_image is a file ID (like 'im_mzp-QKbW.jpeg')
                elif input_image.startswith('im_') and ('.' in input_image):
                    # It's a file ID, need to convert to base64
                    try:
                        file_path = os.path.join(FILES_DIR, input_image)

                        # Ensure the files directory exists
                        os.makedirs(FILES_DIR, exist_ok=True)

                        if os.path.exists(file_path):
                            with open(file_path, 'rb') as f:
                                image_data = f.read()
                                processed_input_image = base64.b64encode(image_data).decode('utf-8')
                        else:
                            print(f"❌ Input image file not found: {file_path}")
                            processed_input_image = None
                    except Exception as e:
                        print(f"❌ Error reading input image file: {e}")
                        processed_input_image = None
                elif input_image.startswith('data:'):
                    # It's already a data URL, extract base64 part
                    processed_input_image = input_image.split(',')[1] if ',' in input_image else input_image
                else:
                    # Assume it's already base64 encoded
                    processed_input_image = input_image

            # Generate image using async generator (直接使用 await)
            try:
                # Prepare context for generator
                generation_ctx = {
                    'session_id': session_id,
                    'tool_call_id': tool_call_id
                }

                # Add multi-image context if available
                if multi_image_context:
                    generation_ctx['multi_images'] = multi_image_context
                # If override requests kontext-multiple, auto-collect recent images for multi-image workflow
                try:
                    if isinstance(model, str) and ('kontext' in model.lower()) and ('multiple' in model.lower()) and 'multi_images' not in generation_ctx:
                        eff_uid = user_id
                        if not eff_uid:
                            try:
                                from services.strands_context import get_user_id as _get_uid
                                eff_uid = _get_uid()
                            except Exception:
                                pass
                        recent = get_recent_images_from_session(session_id, eff_uid, count=3)
                        if len(recent) >= 2:
                            generation_ctx['multi_images'] = { 'images': recent[:3] }
                            print(f"🔍 DEBUG: Auto-attached {len(generation_ctx['multi_images']['images'])} images for kontext-multiple workflow")
                        else:
                            print("⚠️ Not enough recent images for kontext-multiple, will fall back to single workflow")
                except Exception as _e:
                    print(f"⚠️ Failed to prepare multi-images context: {_e}")

                    print(f"🔍 DEBUG: Passing multi-image context to generator")

                file_id, width, height, file_path = await generator.generate(
                    prompt=prompt,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    input_image=processed_input_image,
                    ctx=generation_ctx
                )
            except Exception as e:
                print(f"❌ Image generation error: {e}")
                raise e

            print(f"✅ Generated image: {file_id} ({width}x{height})")

            # Save to database using synchronous operations
            try:
                # Use UserContextManager to set the correct user context for database operations
                effective_user_id = user_id
                if not effective_user_id:
                    # Fallback: try to get user_id from strands context
                    try:
                        from services.strands_context import get_user_id
                        effective_user_id = get_user_id()
                    except Exception:
                        pass

                if effective_user_id:
                    from services.user_context import UserContextManager
                    with UserContextManager(effective_user_id):
                        # 保存文件记录
                        db_service.create_file(file_id, file_path, width, height)

                        # 保存图像消息
                        if session_id:
                            # Create image message for database
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

                            db_service.create_message(session_id, 'assistant', json.dumps(image_message))
                else:
                    print(f"⚠️ No user_id available for database operations, skipping database save")

                # Always broadcast file_generated event to websocket (regardless of database save status)
                # 检查是否已经发送过这个file_generated事件
                file_event_key = f"file_generated_{session_id}_{file_id}_{tool_call_id}"
                if file_event_key in _sent_file_events:
                    print(f"🔄 Skipping duplicate file_generated event: {file_id}")
                else:
                    _sent_file_events.add(file_event_key)
                    message_data = {
                        'type': 'file_generated',
                        'file_id': file_id,
                        'file_path': file_path,
                        'width': width,
                        'height': height,
                        'tool_call_id': tool_call_id
                    }
                    print(f"🔍 DEBUG: Broadcasting file_generated message: {message_data}")
                    await broadcast_session_update(session_id, canvas_id, message_data, effective_user_id)
                    print(f"🔍 DEBUG: Successfully broadcasted file_generated message for session {session_id}")

            except Exception as db_error:
                print(f"🔍 DEBUG: Database save error: {db_error}")
                traceback.print_exc()

            return f"Image generated successfully! File ID: {file_id}, Size: {width}x{height}. The image has been saved and is ready for use."

        except Exception as e:
            print(f"Error generating image: {e}")
            traceback.print_exc()
            return f"Failed to generate image: {str(e)}"

    return generate_image_with_context


def generate_image_id():
    """生成图像ID"""
    return generate_file_id()


# 添加一个虚拟的工具函数来满足 strands 库的期望
# 这可以防止 "tool function missing" 警告
@tool
def strands_image_generators(
    message: Annotated[str, Field(description="Placeholder message")] = "This is a placeholder tool"
) -> str:
    """
    这是一个占位符工具，用于防止 strands 库的 "tool function missing" 警告。
    实际的图像生成功能由 create_generate_image_with_context 函数提供。

    Args:
        message: 占位符消息

    Returns:
        说明信息
    """
    return "This is a placeholder tool. Use create_generate_image_with_context for actual image generation."
