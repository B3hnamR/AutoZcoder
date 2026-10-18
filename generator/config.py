import os
from pathlib import Path

_out = os.environ.get('ZAI_OUTPUT_DIR', 'output')

CONFIG = {
    'url': 'https://chat.z.ai/auth',
    'headless': False,
    'count': 1,
    'viewport_width': 1280,
    'viewport_height': 720,
    'timeout': 30000,
    'max_captcha_attempts': 15,
    'max_refresh_per_captcha': 3,
    'drag_base_steps': 50,
    'wait_after_drag': 2.0,
    'wait_after_refresh': 1.5,
    'min_hole_x_native': 50,
    'template_match_threshold': 0.12,
    'distance_voting_tolerance': 8.0,
    'min_confidence_threshold': 0.0,
    'calibration_offsets': [-4, -2, 2, 4, -6, 6, -8, 8],
    'auto_calibrate': False,
    'calibration_memory': 5,
    'enable_adaptive_offset': True,
    'enable_variance_method': True,
    'enable_cv2_gray': True,
    'enable_energy_methods': True,
    'result_poll_count': 120,
    'max_drag_corrections': 5,
    'drag_multiplier': 1.0,
    'drag_dead_zone': 60.0,
    'drag_ratio': 1.0,
    'mail_domains': [],            # extra personal domains; ZAI_MAIL_DOMAIN picks one per signup
    'mail_domain': 'behnamrjd.me', # default/fallback domain
    'manual_captcha': False,       # True (or env ZAI_MANUAL=1): you solve the slider by hand
    'proxy': None,                 # single proxy URL, or use env ZAI_PROXY
    'proxy_retries': 5,            # auto-rotate to a fresh proxy on failure (proxy mode only)
    'invite_code': 'MT9GNDQ8',     # referral Global Build claim; '' disables, ZAI_INVITE_CODE overrides
    'mail_inbox_url': None,        # e.g. https://zai-mail.<you>.workers.dev/inbox
    'mail_inbox_secret': None,     # same value as INBOX_SECRET in the worker
    'max_acceptable_spread': 30.0,
    'min_acceptable_confidence': 0.40,
    'drag_easing': 'smoothstep',
    'drag_wobble': 0.5,
    'drag_wobble_freq': 2.5,
    'micro_jitter': 0.2,
    'hover_before_drag': True,
    'hover_count': (3, 6),
    'log_level': 'INFO',
    'log_to_file': True,
    'log_to_console': True,
    'log_file': os.path.join(_out, 'output.txt'),
    'save_screenshots': True,
    'save_captcha_images': True,
    'track_timing': True,
    'log_dir': _out,
    'screenshot_dir': _out,
    'data_dir': _out,
    'output_dir': _out,
    'debug_dir': _out,
}
