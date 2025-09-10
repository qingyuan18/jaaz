import type { LLMConfig, ToolCallFunctionName } from '@/types/types'

// 获取 API 基础 URL
const getBaseApiUrl = () => {
  if (typeof window === 'undefined') {
    // 服务端渲染时的默认值
    return 'http://localhost:57988'
  }

  const hostname = window.location.hostname

  // 如果当前访问的是 EC2 域名，则 API 也使用 EC2 域名
  if (hostname.includes('amazonaws.com') || hostname.includes('ec2-')) {
    return 'http://ec2-52-24-176-86.us-west-2.compute.amazonaws.com:57988'
  }

  // 本地开发环境
  if (hostname === 'localhost' || hostname === '127.0.0.1') {
    return 'http://localhost:57988'
  }

  // 生产环境
  return 'https://jaaz.app'
}

// API Configuration
export const BASE_API_URL = getBaseApiUrl()

export const PROVIDER_NAME_MAPPING: {
  [key: string]: { name: string; icon: string }
} = {
  jaaz: {
    name: 'open gallary',
    icon: 'https://raw.githubusercontent.com/11cafe/jaaz/refs/heads/main/assets/icons/jaaz.png',
  },
  anthropic: {
    name: 'Claude',
    icon: 'https://registry.npmmirror.com/@lobehub/icons-static-png/latest/files/dark/claude-color.png',
  },
  openai: { name: 'OpenAI', icon: 'https://openai.com/favicon.ico' },
  replicate: {
    name: 'Replicate',
    icon: 'https://images.seeklogo.com/logo-png/61/1/replicate-icon-logo-png_seeklogo-611690.png',
  },
  ollama: {
    name: 'Ollama',
    icon: 'https://images.seeklogo.com/logo-png/59/1/ollama-logo-png_seeklogo-593420.png',
  },
  huggingface: {
    name: 'Hugging Face',
    icon: 'https://huggingface.co/favicon.ico',
  },
  wavespeed: {
    name: 'WaveSpeedAi',
    icon: 'https://www.wavespeed.ai/favicon.ico',
  },
  comfyui: {
    name: 'ComfyUI',
    icon: 'https://framerusercontent.com/images/3cNQMWKzIhIrQ5KErBm7dSmbd2w.png',
  },
  bedrock: {
    name: 'AWS Bedrock',
    icon: 'https://a0.awsstatic.com/libra-css/images/logos/aws_logo_smile_1200x630.png',
  },
  siliconflow: {
    name: 'SiliconFlow',
    icon: 'https://siliconflow.cn/favicon.ico',
  },
}
export const DEFAULT_PROVIDERS_CONFIG: { [key: string]: LLMConfig } = {
  anthropic: {
    models: {
      'claude-3-7-sonnet-latest': { type: 'text' },
    },
    url: 'https://api.anthropic.com/v1/',
    api_key: '',
    max_tokens: 8192,
  },
  openai: {
    models: {
      'gpt-4o': { type: 'text' },
      'gpt-4o-mini': { type: 'text' },
      'gpt-image-1': { type: 'image' },
    },
    url: 'https://api.openai.com/v1/',
    api_key: '',
    max_tokens: 8192,
  },
  replicate: {
    models: {
      'google/imagen-4': { type: 'image' },
      'black-forest-labs/flux-1.1-pro': { type: 'image' },
      'black-forest-labs/flux-kontext-pro': { type: 'image' },
      'black-forest-labs/flux-kontext-max': { type: 'image' },
      'recraft-ai/recraft-v3': { type: 'image' },
      'stability-ai/sdxl': { type: 'image' },
    },
    url: 'https://api.replicate.com/v1/',
    api_key: '',
    max_tokens: 8192,
  },
  jaaz: {
    models: {
      // text models
      'gpt-4o': { type: 'text' },
      'gpt-4o-mini': { type: 'text' },
      'deepseek/deepseek-chat-v3-0324:free': { type: 'text' },
      'deepseek/deepseek-chat-v3-0324': { type: 'text' },
      'anthropic/claude-sonnet-4': { type: 'text' },
      'anthropic/claude-3.7-sonnet': { type: 'text' },
      // image models
      'google/imagen-4': { type: 'image' },
      // 'google/imagen-4-ultra': { type: 'image' },
      'black-forest-labs/flux-1.1-pro': { type: 'image' },
      'black-forest-labs/flux-kontext-pro': { type: 'image' },
      'black-forest-labs/flux-kontext-max': { type: 'image' },
      'recraft-ai/recraft-v3': { type: 'image' },
      // 'ideogram-ai/ideogram-v3-balanced': { type: 'image' },
      'openai/gpt-image-1': { type: 'image' },
    },
    url: `${BASE_API_URL}/api/v1/`,
    api_key: '',
    max_tokens: 8192,
  },
  wavespeed: {
    models: {
      'wavespeed-ai/flux-dev': { type: 'image' },
    },
    url: 'https://api.wavespeed.ai/api/v3/',
    api_key: '',
  },
  comfyui: {
    models: {
      'flux-kontext': { type: 'comfyui', media_type: 'image' },
      'flux-kontext-multiple': { type: 'comfyui', media_type: 'image' },
      'flux-t2i': { type: 'comfyui', media_type: 'image' },
      'wan-t2v': { type: 'comfyui', media_type: 'video' },
      'wan-i2v': { type: 'comfyui', media_type: 'video' },
    },
    url: 'http://ec2-34-216-22-132.us-west-2.compute.amazonaws.com:8188',
    api_key: '',
  },
  // huggingface: {
  //   models: {
  //     "dreamlike-art/dreamlike-photoreal-2.0": { type: "image" },
  //   },
  //   url: "https://api.replicate.com/v1/",
  //   api_key: "",
  // },
  bedrock: {
    models: {
      'us.anthropic.claude-3-7-sonnet-20250219-v1:0': { type: 'text' },
    },
    url: '',
    api_key: '',
    max_tokens: 8192,
    region: 'us-west-2',
  },
  siliconflow: {
    models: {
      'deepseek-ai/DeepSeek-V3': { type: 'text' },
    },
    url: 'https://api.siliconflow.cn/v1/',
    api_key: '',
    max_tokens: 8192,
  },
}

export const DEFAULT_MODEL_LIST = Object.keys(DEFAULT_PROVIDERS_CONFIG).flatMap(
  (provider) =>
    Object.keys(DEFAULT_PROVIDERS_CONFIG[provider].models).map((model) => {
      const modelConfig = DEFAULT_PROVIDERS_CONFIG[provider].models[model]
      return {
        provider,
        model,
        type: modelConfig.type ?? 'text',
        media_type: modelConfig.media_type,
        url: DEFAULT_PROVIDERS_CONFIG[provider].url,
      }
    })
)

// Tool call name mapping
export const TOOL_CALL_NAME_MAPPING: { [key in ToolCallFunctionName]: string } =
  {
    generate_image: 'Generate Image',
    prompt_user_multi_choice: 'Prompt Multi-Choice',
    prompt_user_single_choice: 'Prompt Single-Choice',
    write_plan: 'Write Plan',
    finish: 'Finish',
  }

export const LOGO_URL =
  'https://raw.githubusercontent.com/11cafe/jaaz/refs/heads/main/assets/icons/jaaz.png'

export const DEFAULT_SYSTEM_PROMPT = `You are a professional art design agent. You can write very professional image prompts to generate aesthetically pleasing images that best fulfilling and matching the user's request.
Step 1. write a design strategy plan. Write in the same language as the user's inital first prompt.

Example Design Strategy Doc:
Design Proposal for “MUSE MODULAR – Future of Identity” Cover
• Recommended resolution: 1024 × 1536 px (portrait) – optimal for a standard magazine trim while preserving detail for holographic accents.

• Style & Mood
– High-contrast grayscale base evoking timeless editorial sophistication.
– Holographic iridescence selectively applied (cyan → violet → lime) for mask edges, title glyphs and micro-glitches, signalling futurism and fluid identity.
– Atmosphere: enigmatic, cerebral, slightly unsettling yet glamorous.

• Key Visual Element
– Central androgynous model, shoulders-up, lit with soft frontal key and twin rim lights.
– A translucent polygonal AR mask overlays the face; within it, three offset “ghost” facial layers (different eyes, nose, mouth) hint at multiple personas.
– Subtle pixel sorting/glitch streaks emanate from mask edges, blending into background grid.

• Composition & Layout

Masthead “MUSE MODULAR” across the top, extra-condensed modular sans serif; characters constructed from repeating geometric units. Spot UV + holo foil.
Tagline “Who are you today?” centered beneath masthead in ultra-light italic.
Subject’s gaze directly engages reader; head breaks the baseline of the masthead for depth.
Bottom left kicker “Future of Identity Issue” in tiny monospaced capitals.
Discreet modular grid lines and data glyphs fade into matte charcoal background, preserving negative space.
• Color Palette
#000000, #1a1a1a, #4d4d4d, #d9d9d9 + holographic gradient (#00eaff, #c400ff, #38ffab).

• Typography
– Masthead: custom variable sans with removable modules.
– Tagline: thin italic grotesque.
– Secondary copy: 10 pt monospaced to reference code.

• Print Finishing
– Soft-touch matte laminate overall.
– Spot UV + holographic foil on masthead, mask outline and glitch shards.

Step 2. Call generate_image tool to generate the image based on the plan immediately, use a detailed and professional image prompt according to your design strategy plan, no need to ask for user's approval. 
`
