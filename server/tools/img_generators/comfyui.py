from typing import Optional, Dict, Any
import os
import json
import sys
import copy
import random
import traceback
try:
    from .base import ImageGenerator, get_image_info_and_save, generate_image_id
except ImportError:
    # 使用绝对导入作为备用
    from tools.img_generators.base import ImageGenerator, get_image_info_and_save, generate_image_id
from services.config_service import config_service, FILES_DIR
from routers.comfyui_execution import execute


def get_asset_path(filename):
    # To get the correct path for pyinstaller bundled application
    if getattr(sys, 'frozen', False):
        # If the application is run as a bundle, the path is relative to the executable
        base_path = sys._MEIPASS
    else:
        # If the application is run in a normal Python environment
        base_path = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))

    return os.path.join(base_path, 'asset', filename)


class ComfyUIGenerator(ImageGenerator):
    """ComfyUI image generator implementation"""

    def __init__(self):
        # Load workflows
        asset_dir = get_asset_path('flux_comfy_workflow.json')
        basic_comfy_t2i_workflow = get_asset_path(
            'default_comfy_t2i_workflow.json')
        flux_kontext_workflow = get_asset_path(
            'flux_kontext_workflow.json')
        flux_kontext_multiple_workflow = get_asset_path(
            'flux_kontext_multiple_workflow.json')

        self.flux_comfy_workflow = None
        self.basic_comfy_t2i_workflow = None
        self.flux_kontext_workflow = None
        self.flux_kontext_multiple_workflow = None

        try:
            print(f"🔍 DEBUG: Loading ComfyUI workflow files...")

            self.flux_comfy_workflow = json.load(open(asset_dir, 'r'))
            self.basic_comfy_t2i_workflow = json.load(
                open(basic_comfy_t2i_workflow, 'r'))
            self.flux_kontext_workflow = json.load(
                open(flux_kontext_workflow, 'r'))
            # Try to load multiple workflow, but don't fail if it doesn't exist yet
            try:
                self.flux_kontext_multiple_workflow = json.load(
                    open(flux_kontext_multiple_workflow, 'r'))
                print("✅ Loaded flux-kontext-multiple workflow")
            except FileNotFoundError:
                print("⚠️ flux_kontext_multiple_workflow.json not found, multi-image features will be limited")
                self.flux_kontext_multiple_workflow = None
        except Exception as e:
            traceback.print_exc()

    async def generate(
        self,
        prompt: str,
        model: str,
        aspect_ratio: str = "1:1",
        input_image: Optional[str] = None,
        **kwargs
    ) -> tuple[str, int, int, str]:
        # Get context from kwargs
        ctx = kwargs.get('ctx', {})
        print(f"🎨 ComfyUI generating: {model}")

        api_url = config_service.app_config.get('comfyui', {}).get('url', '')

        if not api_url:
            raise Exception("ComfyUI URL not configured")

        api_url = api_url.replace('http://', '').replace('https://', '')
        host = api_url.split(':')[0]
        port = api_url.split(':')[1]

        # Handle flux-kontext models
        if 'kontext' in model:
            # Check for multi-image workflow
            if 'multiple' in model and ctx.get('multi_images'):
                if not self.flux_kontext_multiple_workflow:
                    print("⚠️ flux-kontext-multiple workflow not available, falling back to single image")
                    print(f"🔍 DEBUG: Executing workflow file: flux_kontext_workflow.json (fallback)")
                    if not self.flux_kontext_workflow:
                        raise Exception('Flux kontext workflow json not found')
                    return await self._run_flux_kontext_workflow(prompt, input_image, host, port, ctx)
                else:
                    print(f"🔍 DEBUG: Executing workflow file: flux_kontext_multiple_workflow.json")
                    return await self._run_flux_kontext_multiple_workflow(prompt, input_image, host, port, ctx)
            else:
                print(f"🔍 DEBUG: Executing workflow file: flux_kontext_workflow.json (single image mode)")
                if not self.flux_kontext_workflow:
                    raise Exception('Flux kontext workflow json not found')
                return await self._run_flux_kontext_workflow(prompt, input_image, host, port, ctx)

        # Handle other flux models
        elif 'flux' in model:
            print(f"🔍 DEBUG: Executing workflow file: flux_comfy_workflow.json")
            if not self.flux_comfy_workflow:
                raise Exception('Flux workflow json not found')
            workflow = copy.deepcopy(self.flux_comfy_workflow)
            workflow['6']['inputs']['text'] = prompt
            seed_val = random.randint(0, 99999999998)
            workflow['31']['inputs']['seed'] = seed_val
            print(f"🔧 Workflow params (flux): aspect_ratio={aspect_ratio}, seed={seed_val}, text_preview={prompt[:80]!r}, ctx_keys={list(ctx.keys())}")
        else:
            print(f"🔍 DEBUG: Executing workflow file: basic_comfy_t2i_workflow.json")
            if not self.basic_comfy_t2i_workflow:
                raise Exception('Basic workflow json not found')
            workflow = copy.deepcopy(self.basic_comfy_t2i_workflow)
            workflow['6']['inputs']['text'] = prompt
            workflow['4']['inputs']['ckpt_name'] = model
            print(f"🔧 Workflow params (basic_t2i): aspect_ratio={aspect_ratio}, text_preview={prompt[:80]!r}, model={model}, ctx_keys={list(ctx.keys())}")

        execution = await execute(workflow, host, port, ctx=ctx)

        if not execution.outputs:
            raise Exception("No outputs from ComfyUI execution")

        url = execution.outputs[0]

        # get image dimensions
        image_id = generate_image_id()
        mime_type, width, height, extension = await get_image_info_and_save(
            url, os.path.join(FILES_DIR, f'{image_id}')
        )
        filename = f'{image_id}.{extension}'
        return image_id, width, height, filename

    async def _run_flux_kontext_workflow(self, user_prompt: str, input_image_base64: Optional[str], host: str, port: str, ctx: dict) -> tuple[str, int, int, str]:
        """
        Run flux kontext workflow similar to the provided reference implementation
        """
        print(f"🔍 DEBUG: _run_flux_kontext_workflow called - using flux_kontext_workflow.json")
        workflow = copy.deepcopy(self.flux_kontext_workflow)

        has_input = bool(input_image_base64)
        if input_image_base64:
            workflow['197']['inputs']['image'] = input_image_base64
        else:
            # When no input image is provided, create a simple 1x1 pixel transparent PNG as placeholder
            # This prevents the ETN_LoadImageBase64 node from failing with empty string
            # 1x1 transparent PNG in base64
            placeholder_image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQIHWNgAAIAAAUAAY27m/MAAAAASUVORK5CYII="
            workflow['197']['inputs']['image'] = placeholder_image
            print("🔍 DEBUG: Using placeholder image (no input image provided)")

        workflow['196']['inputs']['text'] = user_prompt
        seed_val = random.randint(0, 99999999998)
        workflow['31']['inputs']['seed'] = seed_val
        try:
            mcount = len((ctx or {}).get('multi_images', {}).get('images', []))
        except Exception:
            mcount = 0
        print(f"🔧 Workflow params (kontext): has_input={has_input}, seed={seed_val}, text_preview={user_prompt[:80]!r}, multi_images={mcount}")

        execution = await execute(workflow, host, port, ctx=ctx)

        if not execution.outputs:
            raise Exception('No outputs from flux kontext workflow')

        url = execution.outputs[0]

        # get image dimensions
        image_id = generate_image_id()
        mime_type, width, height, extension = await get_image_info_and_save(
            url, os.path.join(FILES_DIR, f'{image_id}')
        )
        filename = f'{image_id}.{extension}'
        return image_id, width, height, filename

    async def _run_flux_kontext_multiple_workflow(self, user_prompt: str, input_image_base64: Optional[str], host: str, port: str, ctx: dict) -> tuple[str, int, int, str]:
        """
        Run flux kontext multiple workflow for multi-image fusion
        """
        print(f"🔍 DEBUG: _run_flux_kontext_multiple_workflow called - using flux_kontext_multiple_workflow.json")
        workflow = copy.deepcopy(self.flux_kontext_multiple_workflow)
        multi_images = ctx.get('multi_images', {})

        print(f"🎯 Running flux-kontext-multiple workflow with {len(multi_images.get('images', []))} images")

        # Convert referenced images to base64
        image_data_list = []
        for img_info in multi_images.get('images', []):
            try:
                file_id = img_info['file_id']
                # Load image file and convert to base64
                from services.config_service import FILES_DIR
                import os
                import base64

                # Try to find the file using the same logic as get_file endpoint
                file_path = None

                # First try to get file info from database
                try:
                    from services.db_service import db_service
                    file_id_without_ext = file_id.split('.')[0] if '.' in file_id else file_id
                    file_record = db_service.get_file(file_id_without_ext)
                    if file_record:
                        file_path = os.path.join(FILES_DIR, file_record['file_path'])
                except Exception:
                    pass  # Continue with fallback

                # Fallback: try direct path
                if not file_path or not os.path.exists(file_path):
                    file_path = os.path.join(FILES_DIR, file_id)

                # If file doesn't exist and no extension, try common extensions
                if not os.path.exists(file_path) and '.' not in file_id:
                    for ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
                        test_path = os.path.join(FILES_DIR, f'{file_id}.{ext}')
                        if os.path.exists(test_path):
                            file_path = test_path
                            break

                if os.path.exists(file_path):
                    with open(file_path, 'rb') as f:
                        image_data = f.read()
                        base64_data = base64.b64encode(image_data).decode('utf-8')
                        image_data_list.append({
                            'base64': base64_data,
                            'file_id': file_id,
                            'index': img_info['index']
                        })
                        print(f"✅ Loaded image {img_info['index']}: {file_id}")
                else:
                    print(f"❌ Image file not found: {file_path}")
            except Exception as e:
                print(f"❌ Error loading image {img_info.get('file_id', 'unknown')}: {e}")

        if len(image_data_list) < 2:
            raise Exception("At least 2 images are required for multi-image workflow")

        # Configure workflow nodes
        # 主要图像输入节点 (第1张图像)
        if '197' in workflow:
            workflow['197']['inputs']['image'] = image_data_list[0]['base64']

        # 第二张图像输入节点 (第2张图像)
        if '201' in workflow:
            workflow['201']['inputs']['image'] = image_data_list[1]['base64']

        # 如果有第三张图像
        if len(image_data_list) >= 3 and '199' in workflow:
            workflow['199']['inputs']['image'] = image_data_list[2]['base64']


        # 文本prompt节点
        if '196' in workflow:
            workflow['196']['inputs']['text'] = user_prompt

        # 随机种子
        if '31' in workflow:
            workflow['31']['inputs']['seed'] = random.randint(0, 99999999998)
        try:
            indices = [img.get('index') for img in image_data_list]
        except Exception:
            indices = []
        try:
            seed_val = workflow.get('31', {}).get('inputs', {}).get('seed')
        except Exception:
            seed_val = None
        print(f"🔧 Workflow params (kontext-multiple): images={len(image_data_list)}, indices={indices}, seed={seed_val}, text_preview={user_prompt[:80]!r}")


        execution = await execute(workflow, host, port, ctx=ctx)

        if not execution.outputs:
            raise Exception('No outputs from flux kontext multiple workflow')

        url = execution.outputs[0]

        # get image dimensions
        image_id = generate_image_id()
        mime_type, width, height, extension = await get_image_info_and_save(
            url, os.path.join(FILES_DIR, f'{image_id}')
        )
        filename = f'{image_id}.{extension}'
        return image_id, width, height, filename
