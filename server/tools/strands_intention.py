"""
Strands intention tool (fallback)
将用户请求粗分类为以下模式，并给出建议的 generation_model：
- single_img_edit → flux-kontext
- multiple_img_edit → flux-kontext-multiple
- text_to_image → flux-t2i
- text_to_video → wan-t2i
- image_to_video → wan-i2v
"""
from typing import Dict, Any
from strands import tool
from pydantic import Field
import json

from services.strands_context import get_session_id, get_user_id
from tools.strands_image_generators import get_recent_images_from_session


@tool
def analyze_edit_intention(
    prompt: str = Field(description="用户的图像/视频生成或编辑请求（可包含中文或英文）")
) -> str:
    """
    关键词回退意图识别（不调用LLM），返回 JSON 字符串，示例：
    {
      "mode": "multiple_img_edit",
      "generation_model": "flux-kontext-multiple",
      "available_images": 3,
      "reasoning": "检测到融合关键词并且有至少两张历史图像"
    }
    """
    try:
        session_id = get_session_id()
        user_id = get_user_id()
        # 查看最近历史图像数量（最多取3张用于判断）
        recent_images = get_recent_images_from_session(session_id, user_id, count=3)
        available_images = len(recent_images)

        p = (prompt or "").lower()

        # 关键词规则
        multi_keywords = [
            '融合', '合并', '结合', '混合', '组合', '叠加',
            'blend', 'mix', 'combine', 'merge', 'fusion'
        ]
        style_keywords = ['风格', '样式', 'style', 'transfer', '迁移', 'apply']
        video_keywords = ['视频', 'video', 't2v', 'i2v']
        single_keywords = ['修改', '编辑', '改变', '调整', '优化', '美化', '戴上', '带上', '佩戴', 'remove', 'edit', 'change', 'adjust', 'replace']

        explicit_multi_patterns = [
            '第一张', '第二张', '第三张', '图1', '图2', '图3', 'image 1', 'image 2', 'image 3', 'first image', 'second image', 'third image'
        ]

        has_multi_intent = any(k in p for k in multi_keywords) or sum(1 for k in explicit_multi_patterns if k in prompt) >= 2
        has_style_intent = any(k in p for k in style_keywords)
        wants_video = any(k in p for k in video_keywords)

        result: Dict[str, Any] = {
            "mode": "text_to_image",
            "generation_model": "flux-t2i",
            "available_images": available_images,
            "reasoning": ""
        }

        if wants_video:
            # 视频意图
            if available_images >= 1 and ('i2v' in p or '图' in prompt or 'image' in p or 'photo' in p):
                result.update({
                    "mode": "image_to_video",
                    "generation_model": "wan-i2v",
                    "reasoning": "检测到视频意图，且存在图像，可走图生视频"
                })
            else:
                result.update({
                    "mode": "text_to_video",
                    "generation_model": "wan-t2i",
                    "reasoning": "检测到视频意图，将进行文本生视频"
                })
        else:
            # 图像意图
            if available_images >= 2 and (has_multi_intent or has_style_intent):
                result.update({
                    "mode": "multiple_img_edit",
                    "generation_model": "flux-kontext-multiple",
                    "reasoning": "检测到融合或风格迁移意图，且有至少两张历史图像"
                })
            elif available_images >= 1 and (any(k in p for k in single_keywords) or '图' in prompt or 'image' in p or 'photo' in p):
                result.update({
                    "mode": "single_img_edit",
                    "generation_model": "flux-kontext",
                    "reasoning": "检测到单图编辑意图或存在历史图像可用于编辑"
                })
            else:
                result.update({
                    "mode": "text_to_image",
                    "generation_model": "flux-t2i",
                    "reasoning": "未检测到明确的编辑/融合意图，走文本到图像"
                })

        # concise intention debug summary
        try:
            print(f"🎯 Intention result: mode={result.get('mode')}, generation_model={result.get('generation_model')}, available_images={result.get('available_images')}")
        except Exception:
            pass

        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "mode": "text_to_image",
            "generation_model": "flux-t2i",
            "available_images": 0,
            "reasoning": f"意图分析异常: {e}"
        }, ensure_ascii=False)

