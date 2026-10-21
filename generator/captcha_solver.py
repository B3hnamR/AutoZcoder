import time
import os
import random
import math
import logging
import base64
import io
import hashlib
import json
from typing import Optional, Dict, Tuple, List, Any
from dataclasses import dataclass, field
from playwright.sync_api import Page

try:
    import requests
except ImportError:
    requests = None

try:
    import cv2
    import numpy as np
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False
    np = None

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


@dataclass
class MovementPoint:
    x: float
    y: float
    timestamp: float


@dataclass
class DistanceResult:
    method: str
    distance: float
    hole_x1: float = 0.0
    hole_x2: float = 0.0
    confidence: float = 1.0


@dataclass
class VoteResult:
    distance: float
    confidence: float
    methods: Dict[str, float] = field(default_factory=dict)
    spread: float = 0.0


class HumanBehaviorSimulator:

    def __init__(self, page: Page):
        self.page = page
        self._last_position = None
        self._viewport = page.viewport_size or {'width': 1280, 'height': 720}
        self._stored_x: Optional[float] = None
        self._stored_y: Optional[float] = None

    def _get_current_position(self) -> Tuple[float, float]:
        if self._stored_x is not None and self._stored_y is not None:
            return (self._stored_x, self._stored_y)
        return (self._viewport['width'] // 2, self._viewport['height'] // 2)

    def _track_move(self, x: float, y: float):
        self._stored_x = x
        self._stored_y = y

    def _generate_bezier_curve(self, start: Tuple[float, float],
                               end: Tuple[float, float],
                               num_points: int = None) -> List[Tuple[float, float]]:
        if num_points is None:
            num_points = random.randint(25, 45)

        cx1 = start[0] + (end[0] - start[0]) * random.uniform(0.2, 0.4) + random.uniform(-30, 30)
        cy1 = start[1] + (end[1] - start[1]) * random.uniform(0.2, 0.4) + random.uniform(-20, 20)
        cx2 = start[0] + (end[0] - start[0]) * random.uniform(0.6, 0.8) + random.uniform(-30, 30)
        cy2 = start[1] + (end[1] - start[1]) * random.uniform(0.6, 0.8) + random.uniform(-20, 20)

        points = []
        for i in range(num_points + 1):
            t = i / num_points
            eased_t = t * t * (3 - 2 * t)
            x = ((1 - eased_t) ** 3 * start[0]
                 + 3 * (1 - eased_t) ** 2 * eased_t * cx1
                 + 3 * (1 - eased_t) * eased_t ** 2 * cx2
                 + eased_t ** 3 * end[0])
            y = ((1 - eased_t) ** 3 * start[1]
                 + 3 * (1 - eased_t) ** 2 * eased_t * cy1
                 + 3 * (1 - eased_t) * eased_t ** 2 * cy2
                 + eased_t ** 3 * end[1])
            x += random.uniform(-0.5, 0.5)
            y += random.uniform(-0.5, 0.5)
            points.append((x, y))
        return points

    def _get_random_start_position(self) -> Tuple[float, float]:
        x = random.randint(self._viewport['width'] // 4, self._viewport['width'] * 3 // 4)
        y = random.randint(self._viewport['height'] // 2, self._viewport['height'] * 9 // 10)
        if self._stored_x is not None and self._stored_y is not None:
            x = self._stored_x + random.randint(-50, 50)
            y = self._stored_y + random.randint(-50, 50)
            x = max(0, min(self._viewport['width'], x))
            y = max(0, min(self._viewport['height'], y))
        return x, y

    def move_to(self, target_x: float, target_y: float,
                human: bool = True, hesitate: bool = False) -> None:
        start_x, start_y = self._get_current_position()

        if start_x == 0 and start_y == 0:
            start_x, start_y = self._get_random_start_position()
            self.page.mouse.move(start_x, start_y)
            self._track_move(start_x, start_y)
            time.sleep(random.uniform(0.1, 0.5))

        if not human:
            self.page.mouse.move(target_x, target_y)
            self._track_move(target_x, target_y)
            return

        points = self._generate_bezier_curve((start_x, start_y), (target_x, target_y))
        base_duration = random.uniform(0.3, 0.8)
        step_duration = base_duration / len(points)

        for i, (x, y) in enumerate(points):
            t = i / len(points)
            if t < 0.15:
                delay = step_duration * random.uniform(1.5, 2.5)
            elif t < 0.85:
                delay = step_duration * random.uniform(0.5, 0.8)
            else:
                delay = step_duration * random.uniform(1.2, 2.0)

            if random.random() < 0.01 and 0.2 < t < 0.8:
                time.sleep(random.uniform(0.02, 0.08))

            self.page.mouse.move(x, y)
            self._track_move(x, y)
            time.sleep(delay)

        for _ in range(random.randint(2, 5)):
            adj_x = target_x + random.uniform(-1.5, 1.5)
            adj_y = target_y + random.uniform(-1.5, 1.5)
            self.page.mouse.move(adj_x, adj_y)
            self._track_move(adj_x, adj_y)
            time.sleep(random.uniform(0.01, 0.03))

        self.page.mouse.move(target_x, target_y)
        self._track_move(target_x, target_y)

        if hesitate:
            time.sleep(random.uniform(0.1, 0.4))

    def click_with_offset(self, element_box: Dict,
                          offset_type: str = 'normal') -> Tuple[float, float]:
        if not element_box:
            return 0, 0

        center_x = element_box.get('x', 0) + element_box.get('width', 0) / 2
        center_y = element_box.get('y', 0) + element_box.get('height', 0) / 2
        width = element_box.get('width', 40)
        height = element_box.get('height', 40)

        if offset_type == 'normal':
            offset_x = random.gauss(0, width * 0.15)
            offset_y = random.gauss(0, height * 0.15)
        elif offset_type == 'edge':
            if random.random() < 0.5:
                offset_x = -width * random.uniform(0.3, 0.4)
            else:
                offset_x = width * random.uniform(0.3, 0.4)
            offset_y = random.gauss(0, height * 0.2)
        else:
            offset_x = random.uniform(-width * 0.3, width * 0.3)
            offset_y = random.uniform(-height * 0.3, height * 0.3)

        click_x = center_x + offset_x
        click_y = center_y + offset_y

        click_x = max(element_box.get('x', 0) + 2,
                      min(element_box.get('x', 0) + width - 2, click_x))
        click_y = max(element_box.get('y', 0) + 2,
                      min(element_box.get('y', 0) + height - 2, click_y))

        self.move_to(click_x, click_y, human=True, hesitate=True)
        return click_x, click_y


class DistanceCalculator:

    DEFAULT_CONFIDENCE = {
        'ddddocr_cropped': 0.90,
        'ddddocr_full':    0.85,
        'cv2_edge':        0.85,
        'cv2_gray':        0.80,
        'variance':        0.50,
        'gateway_ai':      0.70,
    }

    def __init__(self, ocr, logger, config: Optional[Dict] = None):
        self.ocr = ocr
        self.logger = logger
        self.config = config or {}
        self._ocr_factory = None      # set when `ocr` is a lazy zero-arg callable
        if callable(ocr) and not hasattr(ocr, 'slide_match'):
            self._ocr_factory = ocr
            self.ocr = None
        self.min_hole_x = self.config.get('min_hole_x_native', 20)
        self.cv2_threshold = self.config.get('template_match_threshold', 0.30)
        self.voting_tolerance = self.config.get('distance_voting_tolerance', 5.0)

    def _need_ocr(self):
        """Import ddddocr on first actual use.

        The gateway-AI path must work without ddddocr installed; eagerly
        importing it in the constructor made the AI-only fallback dead code.
        Returns None (and logs) when ddddocr is missing instead of raising
        out of calculate().
        """
        if self.ocr is None and self._ocr_factory is not None:
            self.ocr = self._ocr_factory()
        if self.ocr is None:
            try:
                return self._get_ocr()
            except RuntimeError as e:
                self.logger.warning(str(e))
                return None
        return self.ocr

    def calculate(self, captcha_data: Dict) -> Optional[VoteResult]:
        """Dispatch by strategy.

        'ai'      — ONLY the gateway-AI judge (one judge, one answer).
        'classic' — ONLY the pre-AI CV ensemble (ddddocr/cv2/variance/energy).
        'auto'    — the hybrid ensemble: CV methods + AI judge fused by the
                    weighted-median vote (the original experiment).
        Manual solving never reaches this method (handled in wait_for_manual_solve).
        """
        strategy = (self.config.get('captcha_strategy', 'auto') or 'auto').lower()
        results: List[DistanceResult] = []
        ai_done = False

        if strategy == 'ai':
            # Single-judge mode: nothing local runs at all — no ddddocr import,
            # no CV, no fusion. The AI's answer IS the candidate list.
            gw_key = self.config.get('gateway_key', '')
            if not gw_key:
                self.logger.warning(
                    "ai strategy: no AI endpoint key configured (ZAI_GATEWAY_KEY "
                    "empty). The gateway sets it for spawned workers; standalone "
                    "runs must export it, or set a custom endpoint in the "
                    "dashboard. Switch strategy to classic/manual to proceed.")
                return None
            if not self.config.get('enable_gateway_ai', True):
                self.logger.warning(
                    "ai strategy selected but gateway AI is disabled "
                    "(enable_gateway_ai off) — nothing to solve with")
                return None
            r = self._method_gateway_ai(captcha_data)
            if not r:
                return None
            return VoteResult(distance=r.distance,
                              confidence=r.confidence,
                              methods={r.method: r.distance},
                              spread=0.0)

        if not _HAS_PIL or not _HAS_CV2:
            # CV stack unavailable: the gateway-AI method still works (it
            # only needs PIL for image prep). Try it FIRST; the ddddocr
            # fallback below stays as the last resort. The shared gateway_ai
            # call at the bottom of this method is skipped via `ai_done`
            # so the same result is never appended (and never fetched) twice.
            ai_done = False
            # classic strategy must NOT consult the AI — that gate was
            # missing, so classic-with-missing-cv2 silently called the judge
            if (strategy in ('auto', 'ai') and _HAS_PIL
                    and self.config.get('enable_gateway_ai', True)):
                r = self._method_gateway_ai(captcha_data)
                ai_done = True
                if r:
                    results.append(r)
            if not results:
                self.logger.warning("PIL/cv2 unavailable — falling back to ddddocr-only")
                try:
                    if self._need_ocr() is None:
                        return None
                    return self._fallback_ddddocr_only(captcha_data)
                except Exception as e:
                    self.logger.warning(f"ddddocr fallback failed: {e}")
                    return None
        else:
            if self._need_ocr() is None:
                results = []
            else:
                r = self._method_ddddocr_cropped(captcha_data)
                if r:
                    results.append(r)

                r = self._method_ddddocr_full(captcha_data)
                if r:
                    results.append(r)

            r = self._method_cv2_edge(captcha_data)
            if r:
                results.append(r)

            r = self._method_cv2_gray(captcha_data)
            if r:
                results.append(r)

            r = self._method_variance(captcha_data)
            if r:
                results.append(r)

            if self.config.get('enable_energy_methods', True):
                r = self._method_energy(captcha_data, 'gradient')
                if r:
                    results.append(r)
                r = self._method_energy(captcha_data, 'laplacian')
                if r:
                    results.append(r)

        # Gateway-AI method: the local gateway's GLM vision model reads the
        # gridded background + enlarged piece and estimates the hole center.
        # On wisp-type puzzles the CV methods all fail; this keeps the vote
        # alive so the proven drag pipeline still gets a target.
        # 'classic' strategy never fuses the AI in; 'auto' does, with a
        # short-circuit: when the local CV methods already agree tightly,
        # skip the (blocking, token-costing) AI call entirely.
        ai_needed = (strategy == 'auto'
                     and self.config.get('enable_gateway_ai', True) and not ai_done)
        if ai_needed and results:
            local = [r.distance for r in results]
            spread = max(local) - min(local)
            agreeing = sum(1 for d in local
                           if abs(d - sorted(local)[len(local) // 2]) <= self.voting_tolerance)
            if spread <= self.voting_tolerance and agreeing == len(local):
                ai_needed = False
                self.logger.debug("gateway_ai: local methods agree — skipping AI call")
        if ai_needed:
            r = self._method_gateway_ai(captcha_data)
            if r:
                results.append(r)

        if not results:
            return None

        return self._vote(results)

    def _ddddocr_hole_center(self, result) -> Optional[Tuple[float, Optional[float]]]:
        """Normalize a ddddocr slide_match result to (hole_center_native_x, confidence).

        ddddocr 1.6 returns {'target': [x, y], 'target_x', 'target_y', 'confidence'}
        (a POINT). Older builds returned a 4-element bbox {'target': [x1, y1, x2, y2]}.
        The upstream solver assumed the bbox form, so on 1.6 both ddddocr methods
        silently returned None (len(target) < 4) and the vote ran without them.
        """
        if not result:
            return None
        conf = result.get('confidence')
        try:
            tx = result.get('target_x')
            if tx is not None:
                return float(tx), conf
            target = result.get('target')
            if not target:
                return None
            if len(target) >= 4:
                return (float(target[0]) + float(target[2])) / 2.0, conf
            if len(target) == 2:
                return float(target[0]), conf
        except (TypeError, ValueError, IndexError):
            return None
        return None

    def _extract_piece_bbox(self, puzzle_img) -> Optional[Tuple[int, int, int, int]]:
        arr = np.array(puzzle_img)
        if arr.ndim != 3:
            return None

        if arr.shape[2] >= 4:
            alpha = arr[:, :, 3]
            mask = alpha > 30
        else:
            gray = arr[:, :, 0]
            mask = gray < 240

        cols = np.any(mask, axis=0)
        rows = np.any(mask, axis=1)
        if not cols.any() or not rows.any():
            return None

        x1 = int(np.argmax(cols))
        y1 = int(np.argmax(rows))
        x2 = int(len(cols) - np.argmax(cols[::-1]))
        y2 = int(len(rows) - np.argmax(rows[::-1]))

        if x2 - x1 < 5 or y2 - y1 < 5:
            return None

        return (x1, y1, x2, y2)

    def _to_drag_distance(self, hole_x1_native: float, hole_x2_native: float,
                          piece_x1_native: float, piece_x2_native: float,
                          data: Dict) -> float:
        bg_natural = data.get('bg_natural_width') or 1
        bg_css = data.get('bg_css_width') or bg_natural
        scale = bg_css / bg_natural if bg_natural else 1.0

        puzzle_natural = data.get('puzzle_natural_width') or 1
        puzzle_css = data.get('puzzle_css_width') or puzzle_natural
        puzzle_scale = puzzle_css / puzzle_natural if puzzle_natural else scale

        puzzle_left_css = data.get('puzzle_left_css', 0.0)

        hole_center_native = (hole_x1_native + hole_x2_native) / 2.0
        hole_center_screen = hole_center_native * scale

        piece_center_native = (piece_x1_native + piece_x2_native) / 2.0
        piece_center_screen = puzzle_left_css + (piece_center_native * puzzle_scale)

        return hole_center_screen - piece_center_screen

    def _method_ddddocr_cropped(self, data: Dict) -> Optional[DistanceResult]:
        try:
            puzzle_img = Image.open(io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            bbox = self._extract_piece_bbox(puzzle_img)
            if not bbox:
                return None
            x1, y1, x2, y2 = bbox

            cropped = puzzle_img.crop((x1, y1, x2, y2))

            pad = 10
            padded = Image.new('RGBA',
                               (cropped.width + pad * 2, cropped.height + pad * 2),
                               (0, 0, 0, 0))
            padded.paste(cropped, (pad, pad))
            buf = io.BytesIO()
            padded.save(buf, format='PNG')
            cropped_bytes = buf.getvalue()

            result = None
            for simple in (False, True):
                try:
                    r = self.ocr.slide_match(cropped_bytes, data['bg_bytes'],
                                             simple_target=simple)
                    if r and ('target' in r or 'target_x' in r):
                        result = r
                        break
                except Exception:
                    continue

            parsed = self._ddddocr_hole_center(result)
            if not parsed:
                return None
            hole_cx, ocr_conf = parsed

            if hole_cx < self.min_hole_x:
                self.logger.debug(f"ddddocr_cropped: rejected origin match x={hole_cx}")
                return None

            hole_x1 = hole_x2 = hole_cx
            distance = self._to_drag_distance(hole_x1, hole_x2, x1, x2, data)
            self.logger.debug(f"ddddocr_cropped: x={hole_cx:.1f} ocr_conf={ocr_conf}")
            weight = self.DEFAULT_CONFIDENCE['ddddocr_cropped']
            if ocr_conf is not None and ocr_conf < 0.05:
                weight *= 0.5  # ddddocr is unsure; don't let it dominate the vote
            return DistanceResult(
                method='ddddocr_cropped',
                distance=distance,
                hole_x1=float(hole_x1),
                hole_x2=float(hole_x2),
                confidence=weight,
            )
        except Exception as e:
            self.logger.debug(f"ddddocr_cropped failed: {e}")
            return None

    def _method_ddddocr_full(self, data: Dict) -> Optional[DistanceResult]:
        try:
            puzzle_img = Image.open(io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            bbox = self._extract_piece_bbox(puzzle_img)
            if not bbox:
                return None
            x1, y1, x2, y2 = bbox

            result = self.ocr.slide_match(data['shadow_bytes'], data['bg_bytes'],
                                          simple_target=False)

            parsed = self._ddddocr_hole_center(result)
            if not parsed:
                return None
            hole_cx, ocr_conf = parsed

            if hole_cx < self.min_hole_x:
                return None

            hole_x1 = hole_x2 = hole_cx
            distance = self._to_drag_distance(hole_x1, hole_x2, x1, x2, data)
            self.logger.debug(f"ddddocr_full: x={hole_cx:.1f} ocr_conf={ocr_conf}")
            weight = self.DEFAULT_CONFIDENCE['ddddocr_full']
            if ocr_conf is not None and ocr_conf < 0.05:
                weight *= 0.5
            return DistanceResult(
                method='ddddocr_full',
                distance=distance,
                hole_x1=float(hole_x1),
                hole_x2=float(hole_x2),
                confidence=weight,
            )
        except Exception as e:
            self.logger.debug(f"ddddocr_full failed: {e}")
            return None

    def _method_cv2_edge(self, data: Dict) -> Optional[DistanceResult]:
        if not _HAS_CV2:
            return None
        try:
            puzzle_img = Image.open(io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            arr = np.array(puzzle_img)
            if arr.ndim != 3 or arr.shape[2] < 4:
                return None
            alpha = arr[:, :, 3]

            bbox = self._extract_piece_bbox(puzzle_img)
            if not bbox:
                return None
            x1, y1, x2, y2 = bbox

            piece_edges = cv2.Canny(alpha, 50, 150)

            bg_arr = np.array(Image.open(io.BytesIO(data['bg_bytes'])).convert('RGB'))
            bg_gray = cv2.cvtColor(bg_arr, cv2.COLOR_RGB2GRAY)
            bg_edges = cv2.Canny(bg_gray, 50, 150)

            if piece_edges.max() == 0 or bg_edges.max() == 0:
                return None

            (ph, pw) = piece_edges.shape[:2]
            (bh, bw) = bg_edges.shape[:2]
            if ph > bh or pw > bw:
                return None

            res = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            if max_val < self.cv2_threshold:
                self.logger.debug(f"cv2_edge: low confidence {max_val:.3f}")
                return None

            piece_w = x2 - x1
            hole_x1 = float(max_loc[0] + x1)
            hole_x2 = hole_x1 + piece_w

            if hole_x1 < self.min_hole_x:
                return None

            distance = self._to_drag_distance(hole_x1, hole_x2, x1, x2, data)
            return DistanceResult(
                method='cv2_edge',
                distance=distance,
                hole_x1=hole_x1,
                hole_x2=hole_x2,
                confidence=max(0.4, min(1.0, (max_val - 0.2) / 0.5)),
            )
        except Exception as e:
            self.logger.debug(f"cv2_edge failed: {e}")
            return None

    def _method_cv2_gray(self, data: Dict) -> Optional[DistanceResult]:
        if not self.config.get('enable_cv2_gray', False):
            return None
        if not _HAS_CV2:
            return None
        try:
            puzzle_img = Image.open(io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            arr = np.array(puzzle_img)
            if arr.ndim != 3 or arr.shape[2] < 4:
                return None
            alpha = arr[:, :, 3].astype(np.uint8)

            bbox = self._extract_piece_bbox(puzzle_img)
            if not bbox:
                return None
            x1, y1, x2, y2 = bbox

            bg_arr = np.array(Image.open(io.BytesIO(data['bg_bytes'])).convert('RGB'))
            bg_gray = cv2.cvtColor(bg_arr, cv2.COLOR_RGB2GRAY)
            bg_gray = cv2.GaussianBlur(bg_gray, (3, 3), 0)
            bg_inv = cv2.bitwise_not(bg_gray)

            (ph, pw) = alpha.shape[:2]
            (bh, bw) = bg_inv.shape[:2]
            if ph > bh or pw > bw:
                return None

            res = cv2.matchTemplate(bg_inv, alpha, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            if max_val < self.cv2_threshold:
                self.logger.debug(f"cv2_gray: low confidence {max_val:.3f}")
                return None

            piece_w = x2 - x1
            hole_x1 = float(max_loc[0] + x1)
            hole_x2 = hole_x1 + piece_w

            if hole_x1 < self.min_hole_x:
                return None

            distance = self._to_drag_distance(hole_x1, hole_x2, x1, x2, data)
            return DistanceResult(
                method='cv2_gray',
                distance=distance,
                hole_x1=hole_x1,
                hole_x2=hole_x2,
                confidence=max(0.3, min(0.95, (max_val - 0.2) / 0.5)),
            )
        except Exception as e:
            self.logger.debug(f"cv2_gray failed: {e}")
            return None

    def _method_gateway_ai(self, data: Dict) -> Optional[DistanceResult]:
        """Ask the local gateway's GLM vision model where the hole is.

        Sends the background with a labeled 5% grid + the piece enlarged on
        a light plate; converts the model's percent answer into the same
        native-coordinate distance the other methods produce. Enabled via
        enable_gateway_ai (default True) + a gateway key; config errors,
        network problems, or a non-JSON reply -> None (the vote proceeds
        without it).
        """
        import urllib.request as _ur
        gw_url = self.config.get('gateway_url', 'http://127.0.0.1:8788')
        gw_key = self.config.get('gateway_key', '')
        model = self.config.get('gateway_ai_model', 'GLM-5.3-Flash')
        if not gw_key:
            return None
        if not (_HAS_PIL and data.get('bg_bytes') and data.get('shadow_bytes')):
            return None
        try:
            import io as _io
            from PIL import Image, ImageDraw

            # background + grid (native px), then UPSCALE 2x so the labels
            # are legible for the vision model. The default ~6px bitmap font
            # was unreadable at 300px width and the model miscounted grid
            # lines (live-measured: answered 40% when the hole was at 60%).
            bg = Image.open(_io.BytesIO(data['bg_bytes'])).convert('RGB')
            W, H = bg.size
            UP = 2
            bg = bg.resize((W * UP, H * UP), Image.LANCZOS)
            W, H = bg.size
            dr = ImageDraw.Draw(bg)
            label_font = None
            try:
                from PIL import ImageFont
                label_font = ImageFont.load_default(size=max(16, W // 26))
            except Exception:
                try:
                    from PIL import ImageFont
                    label_font = ImageFont.load_default()
                except Exception:
                    label_font = None

            def _draw_pct(pct, x):
                dr.line([(x, 0), (x, H)], fill=(255, 255, 0), width=2)
                txt = f"{pct}"
                # outlined text: black stroke keeps it readable on any bg
                if label_font is not None:
                    dr.text((x + 4, 4), txt, font=label_font,
                            fill=(255, 255, 0), stroke_width=2,
                            stroke_fill=(0, 0, 0))
                else:
                    dr.text((x + 4, 4), txt, fill=(255, 255, 0))

            # every 10% gets a line+label; 5% marks get a short top tick only
            for pct in range(0, 101, 5):
                x = int(W * pct / 100)
                if pct % 10 == 0:
                    _draw_pct(pct, x)
                else:
                    dr.line([(x, 0), (x, H // 12)], fill=(255, 255, 0), width=1)

            # piece cropped to alpha bbox, on a plate, 4x
            pz = Image.open(_io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            bb = pz.getbbox() or (0, 0, pz.width, pz.height)
            pz = pz.crop(bb)
            plate = Image.new('RGB', pz.size, (245, 245, 245))
            plate.paste(pz, mask=pz.split()[-1])
            plate = plate.resize((plate.width * 4, plate.height * 4))

            def _b64(img):
                buf = _io.BytesIO()
                img.save(buf, format='PNG')
                return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()

            # debug: persist the EXACT images sent to the AI + its answer
            # (sorted layout: screenshots/)
            ai_dbg = None
            if self.config.get('save_captcha_images', True):
                try:
                    dbg_dir = os.path.join(os.environ.get('ZAI_OUTPUT_DIR')
                                           or 'output', 'screenshots')
                    os.makedirs(dbg_dir, exist_ok=True)
                    ts = int(time.time() * 1000)
                    bg_png = os.path.join(dbg_dir, f'ai_bg_{ts}.png')
                    plate_png = os.path.join(dbg_dir, f'ai_piece_{ts}.png')
                    bg.save(bg_png)
                    plate.save(plate_png)
                    ai_dbg = (bg_png, plate_png)
                except Exception as e:
                    self.logger.debug(f"ai debug save failed: {e}")

            prompt = (
                "You are solving an Aliyun slider captcha.\n"
                "Image 1: the captcha background. Vertical yellow lines mark "
                "percentage positions of the image width; the number labels "
                "are printed at the top (every 10). Short top ticks are the "
                "5s between them.\n"
                "Image 2: the puzzle piece (enlarged), cut out of image 1.\n"
                "Find the hole in image 1 where the piece fits (matching "
                "outline and vertical position). Read the nearest label, then "
                "interpolate. Answer with the horizontal position of the "
                "CENTER of the hole as a percentage of image width.\n"
                'Reply with ONLY: {"percent": <number>}'
            )
            # Conditional reasoning_effort: only the gateway's GLM models
            # accept it (registry: low/high/max). OpenAI gpt-4o-mini and
            # most third-party endpoints 400 on it. Send it only when the
            # target IS the local gateway (no custom_ai_url override set).
            is_gateway = not self.config.get('custom_ai_url')
            body = json.dumps({
                'model': model,
                # GLM ALWAYS thinks (zcode_protocol forces it) — 512 total
                # output tokens had to cover thinking + JSON answer and ran
                # out (finish_reason=length, empty content). 4096 leaves
                # room for thinking plus the answer.
                'max_tokens': 4096 if is_gateway else 2048,
                **({'reasoning_effort': 'low'} if is_gateway else {}),
                'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': _b64(bg)}},
                    {'type': 'image_url', 'image_url': {'url': _b64(plate)}},
                ]}],
            }).encode()
            req = _ur.Request(gw_url.rstrip('/') + '/v1/chat/completions', data=body,
                              headers={'Authorization': f'Bearer {gw_key}',
                                       'Content-Type': 'application/json'})
            # Hard wall-clock deadline around the whole call. urlopen's
            # timeout is PER SOCKET OPERATION — the gateway retries upstream
            # overloads for up to ~6 minutes, and a fresh socket op resets
            # the timer. Wrap the request in a worker thread and cap the
            # total wait so one judgement can never stall the drag loop.
            timeout = int(self.config.get('gateway_ai_timeout', 45))
            deadline = time.monotonic() + timeout
            opener = _ur.build_opener(_ur.ProxyHandler({}))  # never route 127.0.0.1 via a system proxy
            import threading as _threading
            box = {}

            def _fetch():
                try:
                    with opener.open(req, timeout=timeout) as resp:
                        box['out'] = json.loads(resp.read().decode('utf-8', 'replace'))
                except Exception as e:  # noqa: BLE001 - transported to caller
                    box['err'] = e

            t = _threading.Thread(target=_fetch, daemon=True)
            t.start()
            while t.is_alive() and time.monotonic() < deadline:
                t.join(0.5)
            if t.is_alive():
                self.logger.warning(
                    f"gateway_ai: no answer within {timeout}s hard deadline — giving up")
                return None
            if 'err' in box:
                raise box['err']
            out = box['out']
            msg = (out.get('choices') or [{}])[0].get('message') or {}
            # answer lives in content; reasoning models may put everything in
            # the reasoning channel when content is empty — check both keys.
            content = msg.get('content') or msg.get('reasoning_content') or msg.get('reasoning') or ''
            js = content[content.find('{'):content.rfind('}') + 1]
            pct = float(json.loads(js)['percent'])
            if not (2.0 <= pct <= 98.0):
                return None
            if ai_dbg:
                self.logger.info(
                    f"    gateway_ai: hole={pct:.1f}% "
                    f"(imgs: {os.path.basename(ai_dbg[0])}, {os.path.basename(ai_dbg[1])})")
                # verdict copy: red line at the AI's answer for human review
                try:
                    from PIL import Image as _PILImage
                    v = _PILImage.open(ai_dbg[0]).convert('RGB')
                    _vd = ImageDraw.Draw(v)
                    _vx = int(v.size[0] * pct / 100.0)
                    _vd.line([(_vx, 0), (_vx, v.size[1])], fill=(255, 0, 0), width=3)
                    _vd.text((_vx + 4, v.size[1] - 14), f'AI:{pct:.0f}%',
                             fill=(255, 0, 0))
                    v.save(os.path.splitext(ai_dbg[0])[0] + '_verdict.png')
                except Exception as e:
                    self.logger.debug(f"verdict save failed: {e}")

            # percent -> hole center in SCREEN px, then subtract the piece's
            # on-screen center: the drag pipeline works in screen coordinates
            # (see _to_drag_distance), so the AI answer must live there too.
            hole_center_screen = pct / 100.0 * float(data['bg_css_width'])
            # Piece centre MUST match every other method (_to_drag_distance):
            # the alpha-bbox centre of the ACTUAL shape, scaled — not the CSS
            # box centre, which includes the sprite's transparent padding and
            # biases every AI drag by that padding's asymmetry.
            pz_img = Image.open(_io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            bb = pz_img.getbbox() or (0, 0, pz_img.width, pz_img.height)
            piece_center_native = (bb[0] + bb[2]) / 2.0
            puzzle_natural = data.get('puzzle_natural_width') or pz_img.width
            puzzle_css = data.get('puzzle_css_width') or puzzle_natural
            puzzle_scale = (puzzle_css / puzzle_natural) if puzzle_natural else 1.0
            piece_center_screen = (float(data.get('puzzle_left_css', 0.0))
                                   + piece_center_native * puzzle_scale)

            # AI-seeded CV refinement: the judge lands within a few percent but
            # rarely pixel-perfect (live-measured ~3% right of the hole).
            # Global CV fails on wisps, but restricted to a narrow window
            # around the AI seed it only has to fine-tune — so run the piece
            # template against that window and shift to the best local match.
            refined = self._ai_seed_refine(data, hole_center_screen)
            if refined is not None:
                self.logger.info(
                    f"    gateway_ai: CV refine {hole_center_screen:.1f} -> {refined:.1f}px")
                hole_center_screen = refined

            distance = hole_center_screen - piece_center_screen
            if distance <= 5:
                return None
            return DistanceResult(method='gateway_ai', distance=distance,
                                  hole_x1=hole_center_screen - 20,
                                  hole_x2=hole_center_screen + 20,
                                  confidence=self.DEFAULT_CONFIDENCE['gateway_ai'])
        except Exception as e:
            # AI judge failures must be visible: this is the single call that
            # decides the whole drag in ai/auto mode, and a silent DEBUG line
            # left users staring at "all distance methods failed" x15.
            self.logger.warning(f"gateway_ai failed: {type(e).__name__}: {e}")
            return None

    def _ai_seed_refine(self, data: Dict, hole_center_screen: float) -> Optional[float]:
        """Fine-tune the AI's coarse hole estimate with local template matching.

        The judge reads percentages off the gridded image, so its answers carry
        an inherent +-2-3% quantization error — enough to land on the hole's
        edge instead of its center. Matching the piece template against ONLY a
        narrow window around the AI seed (instead of the whole background,
        where wisps fool CV) usually recovers the exact center. Returns the
        refined hole center in SCREEN px, or None to keep the AI answer.
        """
        if not _HAS_CV2:
            return None
        try:
            bg = Image.open(io.BytesIO(data['bg_bytes'])).convert('L')
            pz = Image.open(io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            bb = pz.getbbox() or (0, 0, pz.width, pz.height)
            pz = pz.crop(bb)

            bg_w, bg_h = bg.size
            scale = float(data['bg_css_width']) / max(1.0, bg_w)  # native->screen
            win = int(bg_w * 0.12)          # +-12% of width around the seed
            seed_x = int(hole_center_screen / scale)
            x0 = max(0, seed_x - win)
            x1 = min(bg_w, seed_x + win)
            if x1 - x0 < pz.width + 4:
                return None

            crop = np.array(bg.crop((x0, 0, x1, bg_h)), dtype=np.float32)
            tmpl = np.array(pz.convert('L'), dtype=np.float32)
            # mask out the piece's transparent padding
            alpha = np.array(pz.split()[-1], dtype=np.float32) / 255.0

            best_x, best_score = None, -1.0
            # slide vertically only within a small band (the hole is at the
            # piece's own height); full-height search wastes the window
            for dy in range(0, max(1, bg_h - tmpl.shape[0]), 4):
                if dy + tmpl.shape[0] > bg_h:
                    break
                for dx in range(0, crop.shape[1] - tmpl.shape[1] + 1, 2):
                    region = crop[dy:dy + tmpl.shape[0], dx:dx + tmpl.shape[1]]
                    num = float((region * tmpl * alpha).sum())
                    den = float((alpha * (region * region + tmpl * tmpl)).sum()) or 1.0
                    score = 2.0 * num / den
                    if score > best_score:
                        best_score, best_x, _best_dy = score, dx, dy

            if best_x is None or best_score < 0.55:
                return None
            center_native = x0 + best_x + tmpl.shape[1] / 2.0
            refined = center_native * scale
            # sanity: refuse wild corrections (>8% of width) — keep the AI seed
            if abs(refined - hole_center_screen) > float(data['bg_css_width']) * 0.08:
                self.logger.debug(
                    f"ai_seed_refine: rejected {refined:.1f} (too far from "
                    f"{hole_center_screen:.1f})")
                return None
            return refined
        except Exception as e:
            self.logger.debug(f"ai_seed_refine failed: {e}")
            return None

    def _method_variance(self, data: Dict) -> Optional[DistanceResult]:
        if not self.config.get('enable_variance_method', False):
            return None
        if not _HAS_CV2:
            return None
        try:
            bg_arr = np.array(Image.open(io.BytesIO(data['bg_bytes'])).convert('L'))
            if bg_arr.size == 0:
                return None

            col_mean = np.mean(bg_arr, axis=0).astype(np.float32)
            median_val = float(np.median(col_mean))

            dark_margin = 25.0
            dark_threshold = median_val - dark_margin
            dark_cols = col_mean < dark_threshold

            start_col = int(bg_arr.shape[1] * 0.15)
            dark_cols[:start_col] = False

            if not dark_cols.any():
                return self._variance_fallback(data, bg_arr, start_col)

            regions: List[Tuple[int, int]] = []
            in_region = False
            region_start = 0
            for i, is_dark in enumerate(dark_cols):
                if is_dark and not in_region:
                    region_start = i
                    in_region = True
                elif not is_dark and in_region:
                    regions.append((region_start, i - 1))
                    in_region = False
            if in_region:
                regions.append((region_start, len(dark_cols) - 1))

            if not regions:
                return self._variance_fallback(data, bg_arr, start_col)

            regions.sort(key=lambda r: r[1] - r[0], reverse=True)
            first_dark, last_dark = regions[0]
            hole_center_native = (first_dark + last_dark) / 2.0

            puzzle_img = Image.open(io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            bbox = self._extract_piece_bbox(puzzle_img)
            if not bbox:
                return None
            x1, y1, x2, y2 = bbox
            piece_w = x2 - x1

            hole_x1 = float(hole_center_native - piece_w / 2)
            hole_x2 = float(hole_center_native + piece_w / 2)

            if hole_x1 < self.min_hole_x:
                return None

            distance = self._to_drag_distance(hole_x1, hole_x2, x1, x2, data)
            return DistanceResult(
                method='variance',
                distance=distance,
                hole_x1=hole_x1,
                hole_x2=hole_x2,
                confidence=self.DEFAULT_CONFIDENCE['variance'],
            )
        except Exception as e:
            self.logger.debug(f"variance failed: {e}")
            return None

    def _variance_fallback(self, data: Dict, bg_arr: 'np.ndarray',
                           start_col: int) -> Optional[DistanceResult]:
        try:
            col_var = np.var(bg_arr, axis=0).astype(np.float32)
            if start_col >= len(col_var):
                return None
            valid = col_var[start_col:]
            kernel = np.ones(7, dtype=np.float32) / 7.0
            smoothed = np.convolve(valid, kernel, mode='same')
            max_idx = int(np.argmax(smoothed))
            hole_center_native = start_col + max_idx

            puzzle_img = Image.open(io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            bbox = self._extract_piece_bbox(puzzle_img)
            if not bbox:
                return None
            x1, y1, x2, y2 = bbox
            piece_w = x2 - x1
            hole_x1 = float(hole_center_native - piece_w / 2)
            hole_x2 = float(hole_center_native + piece_w / 2)
            if hole_x1 < self.min_hole_x:
                return None
            distance = self._to_drag_distance(hole_x1, hole_x2, x1, x2, data)
            return DistanceResult(
                method='variance',
                distance=distance,
                hole_x1=hole_x1,
                hole_x2=hole_x2,
                confidence=self.DEFAULT_CONFIDENCE['variance'] * 0.7,
            )
        except Exception:
            return None

    def _method_energy(self, data: Dict, kind: str) -> Optional[DistanceResult]:
        """Min column-energy gap finder (gradient / laplacian).

        The AI-inpainted gap is a SMOOTHED region, so it has the LOWEST high-frequency
        energy in the background. Sliding a piece-width window over the smoothed column
        energy and taking the minimum locates the gap. This is the method that stays
        reliable when the piece is a faint, near-transparent wisp (shape matchers
        like cv2_edge/cv2_gray collapse there because a wisp has no usable shape).
        Mirrors the offline `_methods.py` rig.
        """
        if not _HAS_CV2:
            return None
        try:
            bg = cv2.imdecode(np.frombuffer(data['bg_bytes'], np.uint8), cv2.IMREAD_GRAYSCALE)
            if bg is None or bg.size == 0:
                return None
            bg = bg.astype(np.float32)
            H, W = bg.shape

            if kind == 'gradient':
                gx = cv2.Sobel(bg, cv2.CV_32F, 1, 0, 3)
                gy = cv2.Sobel(bg, cv2.CV_32F, 0, 1, 3)
                col_energy = np.sqrt(gx * gx + gy * gy).sum(axis=0) / H
            elif kind == 'laplacian':
                col_energy = np.abs(cv2.Laplacian(bg, cv2.CV_32F)).sum(axis=0) / H
            else:
                return None

            smoothed = np.convolve(col_energy, np.ones(7, dtype=np.float32) / 7.0, mode='same')

            puzzle_img = Image.open(io.BytesIO(data['shadow_bytes'])).convert('RGBA')
            bbox = self._extract_piece_bbox(puzzle_img)
            if not bbox:
                return None
            x1, y1, x2, y2 = bbox
            piece_w = x2 - x1

            lo = int(W * 0.12)
            hi = int(W * 0.88)
            if hi - piece_w <= lo:
                return None

            best_sum = None
            best_x = lo
            for x in range(lo, hi - piece_w):
                s = float(smoothed[x:x + piece_w].sum())
                if best_sum is None or s < best_sum:
                    best_sum = s
                    best_x = x
            if best_x < 10:
                # only reject the absolute origin; the oracle will reject any other
                # wrong candidate. A tighter guard (min_hole_x) silently deleted valid
                # near-left gaps before they could ever be tried.
                return None
            hole_x1 = float(best_x)
            hole_x2 = float(best_x + piece_w)
            distance = self._to_drag_distance(hole_x1, hole_x2, x1, x2, data)
            return DistanceResult(
                method=kind,
                distance=distance,
                hole_x1=hole_x1,
                hole_x2=hole_x2,
                confidence=0.75,
            )
        except Exception as e:
            self.logger.debug(f"{kind} failed: {e}")
            return None

    def _vote(self, results: List[DistanceResult]) -> VoteResult:
        sorted_results = sorted(results, key=lambda r: r.distance)

        if len(sorted_results) == 1:
            r = sorted_results[0]
            return VoteResult(
                distance=r.distance,
                confidence=r.confidence * 0.5,
                methods={r.method: r.distance},
                spread=0.0,
            )

        distances = [r.distance for r in sorted_results]
        weights = [r.confidence for r in sorted_results]

        total_w = sum(weights)
        cumulative = 0.0
        weighted_median = distances[-1]
        for d, w in zip(distances, weights):
            cumulative += w
            if cumulative >= total_w / 2.0:
                weighted_median = d
                break

        spread = max(distances) - min(distances)

        agreeing = sum(1 for d in distances
                       if abs(d - weighted_median) <= self.voting_tolerance)
        confidence = agreeing / len(distances)

        if spread > 25:
            confidence *= 0.4
        elif spread > 15:
            confidence *= 0.7
        elif spread > 8:
            confidence *= 0.9

        methods_dict = {r.method: r.distance for r in sorted_results}

        self.logger.info(
            f"    Vote: median={weighted_median:.2f}px "
            f"spread={spread:.2f} conf={confidence:.2f} "
            f"methods={ {k: round(v, 1) for k, v in methods_dict.items()} }"
        )

        return VoteResult(
            distance=weighted_median,
            confidence=confidence,
            methods=methods_dict,
            spread=spread,
        )

    def _fallback_ddddocr_only(self, data: Dict) -> Optional[VoteResult]:
        try:
            result = self.ocr.slide_match(data['shadow_bytes'], data['bg_bytes'],
                                          simple_target=False)
            parsed = self._ddddocr_hole_center(result)
            if not parsed:
                return None
            hole_cx, ocr_conf = parsed

            if hole_cx < self.min_hole_x:
                return None

            # point form gives the hole CENTER x; treat it as both edges
            hole_x1 = hole_x2 = hole_cx

            puzzle_w = data.get('puzzle_natural_width') or 60
            piece_x1 = int(puzzle_w * 0.25)
            piece_x2 = int(puzzle_w * 0.75)

            distance = self._to_drag_distance(hole_x1, hole_x2,
                                              piece_x1, piece_x2, data)
            return VoteResult(
                distance=distance,
                confidence=0.4,
                methods={'ddddocr_only': distance},
                spread=0.0,
            )
        except Exception as e:
            self.logger.debug(f"Fallback failed: {e}")
            return None


class CaptchaSolver:

    def __init__(self, logger_instance=None, config=None):
        self.logger = logger_instance or logging.getLogger(__name__)
        self.config = config or {}
        self.attempts = 0
        self.consecutive_fails = 0
        self.human: Optional[HumanBehaviorSimulator] = None
        self._page: Optional[Page] = None
        self._start_time = 0
        self._ddddocr = None

        self._adaptive_offset = 0.0
        self._attempt_history: List[Tuple[float, float, bool]] = []

        # oracle-in-the-loop candidate state (see _build_candidates)
        self._candidates: List[float] = []
        self._cand_idx = 0
        self._cand_sig: Optional[Tuple[int, int]] = None
        self._rotate = 0   # persists across puzzles (see _human_solve)

    def _get_ocr(self):
        if self._ddddocr is None:
            try:
                import ddddocr
            except ImportError as e:
                # surface a clear message instead of raising out of
                # _need_ocr()/calculate() — one missing wheel used to kill
                # the whole auto/classic calculation path
                raise RuntimeError(
                    "ddddocr is not installed (pip install ddddocr) — CV "
                    "methods unavailable; use the 'ai' strategy or install it"
                ) from e
            try:
                self._ddddocr = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
            except TypeError:
                self._ddddocr = ddddocr.DdddOcr(det=False, ocr=False)
        return self._ddddocr

    def wait_for_manual_solve(self, page: Page, timeout: int = 300) -> bool:
        """Manual-assist: let the human solve the slider in the visible browser.

        Polls for the same success signals the auto-solver uses (app submitted signup,
        success class, verify page/text, or the widget staying gone) so the rest of the
        pipeline continues automatically once the human passes it.
        """
        self.logger.info("  MANUAL MODE — solve the captcha in the browser window yourself.")
        self.logger.info("  Waiting for the pass (up to %ds)...", timeout)
        deadline = time.time() + timeout
        gone_since = None
        seen_widget = False        # must SEE the widget before "gone" means solved
        no_widget_since = None
        while time.time() < deadline:
            try:
                st = page.evaluate("""
                    () => {
                        if (window.__signupFired) return 'success:signup';
                        if (document.querySelector('.aliyunCaptcha-verify-success')) return 'success:class';
                        if (window.__verifyParam) return 'success:hook';
                        const path = window.location.pathname || '';
                        if (path.startsWith('/auth/verify')) return 'success:verifypage';
                        if (document.body && /verify your email/i.test(document.body.innerText || ''))
                            return 'success:verifytext';
                        if (document.querySelector('.aliyunCaptcha-verify-error') ||
                            (document.body && /verification failed/i.test(document.body.innerText || '')))
                            return 'failed:text';
                        const present = !!(document.querySelector('#aliyunCaptcha-sliding-slider') ||
                                           document.querySelector('#aliyunCaptcha-popup') ||
                                           document.querySelector('[id^="aliyunCaptcha"]'));
                        return present ? 'pending' : 'pending:gone';
                    }
                """)
            except Exception:
                st = 'pending'
            if st.startswith('success'):
                self.logger.info(f"  MANUAL: captcha passed ({st.split(':')[1]})")
                return True
            if st.startswith('failed'):
                self.logger.info(f"  MANUAL: server says failed ({st.split(':')[1]}) — try again")
                gone_since = None
            elif st == 'pending':
                seen_widget = True
                gone_since = None
                no_widget_since = None
            elif st == 'pending:gone':
                if seen_widget:
                    # widget appeared then vanished == solved
                    if gone_since is None:
                        gone_since = time.time()
                    elif time.time() - gone_since >= 3:
                        self.logger.info("  MANUAL: widget gone >3s — treating as passed")
                        return True
                else:
                    # never loaded at all: dead/blocked proxy. Bail fast so the caller
                    # can rotate instead of waiting out the whole timeout.
                    if no_widget_since is None:
                        no_widget_since = time.time()
                    elif time.time() - no_widget_since >= 20:
                        self.logger.warning("  MANUAL: captcha widget never appeared — "
                                            "bad/blocked proxy")
                        return False
            time.sleep(1)
        self.logger.warning("  MANUAL: timed out waiting for the solve")
        return False

    def solve(self, page: Page) -> Optional[str]:
        self._page = page
        self.human = HumanBehaviorSimulator(page)
        self.attempts += 1
        self._start_time = time.time()

        self.logger.info(f"Hybrid Solve v2.14 attempt {self.attempts}")

        max_attempts = self.config.get('max_captcha_attempts', 15)
        min_confidence = self.config.get('min_confidence_threshold', 0.4)

        nodata_streak = 0
        for attempt in range(max_attempts):
            self.logger.info(f"  [{attempt + 1}/{max_attempts}]")

            if attempt > 0:
                time.sleep(random.uniform(1.0, 1.8))
                # After a failed slide the popup closes; without re-opening it every
                # remaining attempt is spent with nothing on screen to drag, silently.
                if not self._ensure_captcha_open(page):
                    self.logger.debug("    captcha not re-openable this attempt")
                    continue

            is_first = (attempt == 0)
            if not self._wait_for_slider_with_reading(page, slow_reading=is_first):
                if not self._ensure_captcha_open(page):
                    time.sleep(0.2)
                    continue

            result = self._human_solve(page, min_confidence)

            if result == 'success':
                self.consecutive_fails = 0
                return self._on_success(page)
            elif result == 'nodata':
                # The widget stopped serving a challenge image. Same IP/edge block as
                # "widget never loaded", just hit after N solves. Grinding on is useless.
                nodata_streak += 1
                if nodata_streak >= 3:
                    self.logger.error(
                        "  Captcha stopped serving challenges (IP/edge block) — aborting run")
                    return 'CAPTCHA_NOT_LOADED'
            elif result == 'failed':
                nodata_streak = 0
                self.consecutive_fails += 1
            elif result == 'refresh':
                nodata_streak = 0
                self._fast_refresh(page)

            time.sleep(0.3)

        self.logger.error("  Failed after all attempts")
        return None

    def _ensure_captcha_open(self, page: Page, timeout: int = 6) -> bool:
        """Make the slider visible. After a failed slide the Aliyun popup closes, so a
        retry MUST re-trigger it — otherwise the attempt is spent with nothing on screen.
        """
        try:
            if page.locator('#aliyunCaptcha-sliding-slider').is_visible(timeout=600):
                return True
        except Exception:
            pass

        triggers = (
            '#aliyunCaptcha-captcha-wrapper',
            '#aliyunCaptcha-captcha-text',
            '#aliyunCaptcha-captcha-img',
            '[id*="aliyunCaptcha-captcha"]',
        )
        reopened = False
        for sel in triggers:
            try:
                el = page.locator(sel)
                if el.count() > 0 and el.first.is_visible():
                    el.first.click(timeout=1500)
                    self.logger.debug(f"    re-opened captcha via {sel}")
                    time.sleep(0.8)
                    reopened = True
                    break
            except Exception:
                continue

        if not reopened:
            # last resort: the submit button re-triggers the captcha
            try:
                btn = page.locator('button:has-text("Create Account")')
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=1500)
                    self.logger.debug("    re-opened captcha via Create Account button")
                    time.sleep(0.8)
            except Exception:
                pass

        try:
            page.locator('#aliyunCaptcha-sliding-slider').wait_for(
                state='visible', timeout=timeout * 1000)
            return True
        except Exception:
            return False

    def _wait_for_slider_with_reading(self, page: Page, timeout: int = 5,
                                      slow_reading: bool = True) -> bool:
        if slow_reading:
            viewport = page.viewport_size
            if viewport and viewport.get('width', 0) > 100:
                for _ in range(random.randint(1, 2)):
                    x = random.randint(viewport['width'] // 3, viewport['width'] * 2 // 3)
                    y = random.randint(viewport['height'] // 4, viewport['height'] // 2)
                    self.human.move_to(x, y, human=True)
                    time.sleep(random.uniform(0.05, 0.1))

        try:
            selectors = ['#aliyunCaptcha-sliding-slider', '[id*="sliding-slider"]']
            for sel in selectors:
                try:
                    page.locator(sel).wait_for(state='visible', timeout=timeout * 1000)
                    time.sleep(0.1)
                    return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    def _fast_refresh(self, page: Page):
        # new puzzle image -> previous candidate list is stale
        self._candidates = []
        self._cand_sig = None
        self._cand_idx = 0
        try:
            time.sleep(0.2)
            refresh = page.locator('#aliyunCaptcha-btn-refresh')
            if refresh.count() > 0 and refresh.is_visible():
                box = refresh.bounding_box()
                if box:
                    cx = box['x'] + box['width'] / 2 + random.uniform(-3, 3)
                    cy = box['y'] + box['height'] / 2 + random.uniform(-3, 3)
                    self.human.move_to(cx, cy, human=True, hesitate=False)
                    page.mouse.click(cx, cy)
        except Exception as e:
            self.logger.debug(f"Refresh error: {e}")

    def _build_candidates(self, vote: VoteResult) -> List[float]:
        """Cluster every method's distance and rank the clusters, best first.

        Ranking: more methods agreeing wins, tie-broken by closeness to the weighted
        median. The result is the ordered list of drag distances we actually try,
        letting the SDK pass/fail — not the vote — decide which was right. This is
        what replaces the old "methods disagree -> refresh" behaviour, which on wisp
        puzzles refreshed forever and never dragged once.
        """
        if not vote or not vote.methods:
            return [vote.distance] if vote else []
        tol = max(self.config.get('distance_voting_tolerance', 8.0), 12.0)
        clusters: List[List[float]] = []
        for d in sorted(vote.methods.values()):
            for c in clusters:
                if abs(d - (sum(c) / len(c))) <= tol:
                    c.append(d)
                    break
            else:
                clusters.append([d])
        clusters.sort(key=lambda c: (-len(c), abs(sum(c) / len(c) - vote.distance)))
        cands = [round(sum(c) / len(c), 1) for c in clusters]
        if all(abs(vote.distance - c) > tol for c in cands):
            cands.insert(0, round(vote.distance, 1))

        # High spread == wisp puzzle == every CV cluster is essentially noise
        # (validated live: the AI judge was right but its cluster ranked 3rd
        # behind garbage, burning the low-risk first attempts). When the AI
        # participated and the spread is this large, trust the AI first.
        # On TIGHT votes the CV cluster still leads, but the AI answer is
        # promoted to rank 2 if it isn't already at/near the top — live data:
        # necklace puzzle, spread=30, CV said 153 / AI said 176; cand #1 (CV)
        # failed, the AI answer was never reached that attempt.
        ai_d = vote.methods.get('gateway_ai')
        if ai_d is not None:
            ai_d = round(float(ai_d), 1)
            if vote.spread > self.config.get('ai_first_spread', 80.0):
                cands = ([ai_d] + [c for c in cands if abs(c - ai_d) > tol]) \
                    or [ai_d]
            elif any(abs(c - ai_d) <= tol for c in cands[:1]):
                pass  # AI already effectively rank 1
            else:
                rest = [c for c in cands if abs(c - ai_d) > tol]
                cands = [cands[0], ai_d] + rest
        return cands

    def _human_solve(self, page: Page, min_confidence: float) -> str:
        try:
            slider = page.locator('#aliyunCaptcha-sliding-slider')
            if slider.count() == 0:
                return 'failed'
            box = slider.bounding_box()
            if not box:
                return 'failed'

            self._human_scan_image(page)
            captcha_data = self._get_captcha_data(page)
            if not captcha_data:
                self.logger.warning("    captcha data unavailable (image not ready/closed)")
                return 'nodata'

            vote = self._calculate_distance_v2(captcha_data)
            if vote is None:
                self.logger.warning("    All distance methods failed — refreshing")
                self._candidates = []
                self._cand_sig = None
                return 'refresh'

            # ---- oracle-in-the-loop: try ranked candidates instead of refreshing ----
            # Real puzzle signature: lengths alone are identical across different
            # puzzles (same dimensions), so candidates were never rebuilt when the
            # site swapped the image. Hash the bytes instead.
            sig = (hashlib.md5(captcha_data['bg_bytes']).hexdigest(),
                   hashlib.md5(captcha_data['shadow_bytes']).hexdigest())
            if self._cand_sig != sig:
                self._candidates = self._build_candidates(vote)
                self._cand_idx = 0
                self._cand_sig = sig
                # Fresh puzzle -> restart rotation. The site serves a NEW image
                # after every failed slide, so carrying `_rotate` across puzzles
                # skipped past the rank-1 (AI) answer of each fresh list before
                # it was ever tried (live: AI=40% correct, but attempt dragged
                # cand #3 of the NEW puzzle instead).
                self._rotate = 0
                self.logger.info(
                    f"    Candidates [{len(self._candidates)}]: "
                    f"{[round(c, 1) for c in self._candidates]} "
                    f"(vote={vote.distance:.1f} spread={vote.spread:.1f} "
                    f"conf={vote.confidence:.2f})"
                )

            if not self._candidates:
                return 'refresh'

            # ROTATE through candidates across attempts. NOTE: the site serves
            # a NEW image after every failed slide, so the fresh-signature
            # branch above normally rebuilds candidates and resets `_rotate`
            # to 0 before we get here — each attempt drags rank-1 of the NEW
            # list. Rotation below only engages when the SAME image survives
            # a failed attempt (rare refresh glitch): then, and only then,
            # advancing to the next candidate is meaningful. The counter is
            # NOT carried across different puzzles (rank-2 of an unrelated
            # image is noise).
            idx = self._rotate % len(self._candidates)
            base_distance = self._candidates[idx] + random.uniform(-0.5, 0.5)
            # Clamp against nonsense AND against the physical track: the drag
            # can never need more than (bg width - piece width) px. The old
            # 10000 default let an 800px outlier drag the mouse out of the
            # window, past the point where the creep loop could correct it.
            bg_css_w = float(captcha_data.get('bg_css_width') or 0)
            puzzle_css_w = float(captcha_data.get('puzzle_css_width') or 0)
            track_max = (bg_css_w - puzzle_css_w) if (bg_css_w > 0 and puzzle_css_w > 0) else None
            max_drag = self.config.get('max_drag_distance', 0) or None
            ceiling = min(x for x in (max_drag, track_max) if x) if (max_drag or track_max) else None
            target = int(min(ceiling, base_distance)) if (base_distance > 3 and ceiling) else (int(base_distance) if base_distance > 3 else None)
            if target is None:
                self.logger.info(
                    f"    Candidate {idx + 1} distance {base_distance:.1f}px out of "
                    f"range — rotating to next candidate"
                )
                self._rotate += 1
                return 'failed'

            self.logger.info(
                f"    Try cand {idx + 1}/{len(self._candidates)} (attempt {self._rotate + 1}): "
                f"target={target}px (vote={vote.distance:.2f}, spread={vote.spread:.2f}, "
                f"methods={len(vote.methods)})"
            )

            slider_cx = box['x'] + box['width'] / 2
            slider_cy = box['y'] + box['height'] / 2

            try:
                piece_before = page.evaluate("""
                    () => {
                        const p = document.querySelector('#aliyunCaptcha-puzzle');
                        if (!p) return null;
                        return p.getBoundingClientRect().x;
                    }
                """)
            except Exception:
                piece_before = None

            drag_dead_zone = self.config.get('drag_dead_zone', 96.0)
            drag_ratio = self.config.get('drag_ratio', 1.52)
            mouse_distance = int(target / drag_ratio + drag_dead_zone)  # preliminary only

            self.logger.info(f"    Drag target={target}px (calibrating slope...)")

            page.mouse.move(slider_cx, slider_cy)
            time.sleep(random.uniform(0.15, 0.3))
            page.mouse.down()
            time.sleep(random.uniform(0.15, 0.25))

            try:
                grabbed = page.locator('#aliyunCaptcha-sliding-slider').bounding_box()
                if grabbed:
                    slider_cx = grabbed['x'] + grabbed['width'] / 2
                    slider_cy = grabbed['y'] + grabbed['height'] / 2
            except Exception:
                pass

            # Conservative initial drag (~75% of the way) then a HUMAN-SHAPED
            # correction: continuous micro-creep toward the target, never a
            # discrete jump. The old loop did `mouse.move(new, steps=10)`
            # corrections — the piece teleported 20-50px backward/forward
            # while the button was held, a textbook bot trajectory that
            # Aliyun's risk engine rejects even when the final position is
            # correct (verified live: piece visually in the hole, failed).
            mouse_distance = int(drag_dead_zone + target * 0.75)
            start_x = slider_cx

            self.logger.info(
                f"    Drag mouse={mouse_distance}px "
                f"(target={target}, dead_zone={drag_dead_zone}, ratio={drag_ratio})")

            end_x = slider_cx + mouse_distance
            end_y = slider_cy + random.uniform(-2, 2)
            num_steps = random.randint(40, 60)
            total_time = random.uniform(0.5, 0.9)

            for i in range(num_steps + 1):
                t = i / num_steps
                eased = 1 - (1 - t) ** 3
                x = start_x + (end_x - start_x) * eased
                y = slider_cy + (end_y - slider_cy) * eased
                y += math.sin(t * math.pi) * random.uniform(-1, 1)
                page.mouse.move(x, y)
                time.sleep(total_time / num_steps * random.uniform(0.8, 1.2))

            time.sleep(random.uniform(0.1, 0.2))

            try:
                def _piece_x():
                    return page.evaluate("""
                        () => {
                            const p = document.querySelector('#aliyunCaptcha-puzzle');
                            if (!p) return null;
                            return p.getBoundingClientRect().x;
                        }
                    """)

                piece_after = _piece_x()
                if piece_after is not None and piece_before is not None:
                    # HUMAN CORRECTION PHASE. After the initial undershoot the
                    # hand creeps forward in a continuous decelerating motion
                    # (like aligning a slider), reading the piece position
                    # between micro-moves. Slope feedback still adapts to the
                    # piece-per-mouse ratio, but every emitted move is small
                    # (<= ~12px) and densely sampled, so the trajectory stays
                    # smooth from press to release.
                    max_corr = int(self.config.get('max_drag_corrections', 5))
                    pieces = [piece_before, piece_after]
                    mouses = [slider_cx, end_x]
                    # split the remaining distance into small continuous steps;
                    # +12px planning margin so the last micro-steps can close a
                    # few-px residual instead of releasing short (live: released
                    # 4.5px early because the planned steps were exhausted)
                    creep_total = (target - (piece_after - piece_before)) * 1.0 + 12.0
                    if creep_total < 0:
                        creep_total = 0.0  # never creep backward; release short instead

                    # split the remaining distance into small continuous steps
                    micro_n = max(6, min(24, int(abs(creep_total) / 2) + 6))
                    # decelerating profile: many steps near the end
                    weights = [(1.0 - (i / micro_n)) ** 0.6 + 0.15
                               for i in range(micro_n)]
                    wsum = sum(weights) or 1.0
                    steps_px = [creep_total * (w / wsum) for w in weights]

                    si = 0
                    cur_x = end_x
                    last_px = piece_after
                    for step in range(max_corr * 8):
                        if si >= micro_n:
                            break
                        moved = pieces[-1] - pieces[0]
                        error = target - moved
                        if step % 3 == 0:
                            self.logger.info(
                                f"    Creep {step}: piece moved {moved:.1f}px "
                                f"(target={target}px, error={error:+.1f}px)")
                        if abs(error) <= 2:
                            break
                        # planned steps exhausted but still >2px short: extend
                        # with one more gentle mini-arc rather than releasing short
                        if si >= micro_n - 1 and abs(error) <= 12:
                            steps_px.append(min(abs(error) + 2.0, 10.0))
                            micro_n += 1

                        # piece pinned (track end / slider wall): stop immediately,
                        # further micro-moves only wiggle the mouse pointlessly
                        if len(pieces) >= 3 and pieces[-1] == pieces[-2] == pieces[-3]:
                            self.logger.info("    Creep: piece pinned — releasing here")
                            break

                        # adaptive slope: shrink/expand remaining micro steps
                        if len(pieces) >= 2 and (mouses[-1] - mouses[-2]) != 0:
                            slope = (pieces[-1] - pieces[-2]) / (mouses[-1] - mouses[-2])
                        else:
                            slope = 1.0
                        if slope <= 0.05:
                            slope = 1.0
                        delta_mouse = steps_px[si] / slope
                        si += 1
                        # hard-cap each emitted move to keep continuity
                        delta_mouse = max(-14.0, min(14.0, delta_mouse))
                        cur_x += delta_mouse

                        # emit as a smooth mini-arc of 1px-ish submoves
                        sub_from = cur_x - delta_mouse
                        sub_n = max(2, min(8, int(abs(delta_mouse))))
                        for k in range(1, sub_n + 1):
                            xx = sub_from + delta_mouse * (k / sub_n)
                            yy = slider_cy + math.sin(k / sub_n * math.pi) * random.uniform(-0.4, 0.4)
                            page.mouse.move(xx, yy)
                            time.sleep(random.uniform(0.012, 0.03))
                        time.sleep(random.uniform(0.04, 0.10))

                        px = _piece_x()
                        if px is None:
                            break
                        pieces.append(px)
                        mouses.append(cur_x)
                        last_px = px
                # small human-like settle + jitter before releasing
                try:
                    slider_now = page.locator('#aliyunCaptcha-sliding-slider').bounding_box()
                    if slider_now:
                        release_x = slider_now['x'] + slider_now['width'] / 2
                        release_y = slider_now['y'] + slider_now['height'] / 2
                        page.mouse.move(release_x + random.uniform(-1.5, 1.5),
                                        release_y + random.uniform(-1.0, 1.0), steps=3)
                        time.sleep(random.uniform(0.12, 0.28))
                except Exception:
                    pass
            except Exception:
                pass

            page.mouse.up()
            time.sleep(random.uniform(0.8, 2.0))

            outcome = self._check_result(page)
            if outcome == 'success':
                self._candidates = []
                self._cand_sig = None
                self._rotate = 0
            elif outcome == 'failed':
                self._cand_idx += 1
                self._rotate += 1
            return outcome

        except Exception as e:
            self.logger.error(f"Solve error: {e}")
            return 'failed'

    def _human_scan_image(self, page: Page):
        try:
            bg = page.locator('#aliyunCaptcha-img')
            if bg.count() > 0:
                box = bg.bounding_box()
                if box:
                    scan_points = [
                        (box['x'] + box['width'] * random.uniform(0.15, 0.35),
                         box['y'] + box['height'] * random.uniform(0.20, 0.40)),
                        (box['x'] + box['width'] * random.uniform(0.60, 0.85),
                         box['y'] + box['height'] * random.uniform(0.20, 0.40)),
                        (box['x'] + box['width'] * random.uniform(0.35, 0.65),
                         box['y'] + box['height'] * random.uniform(0.45, 0.65)),
                        (box['x'] + box['width'] * random.uniform(0.50, 0.80),
                         box['y'] + box['height'] * random.uniform(0.55, 0.75)),
                    ]
                    for px, py in scan_points:
                        self.human.move_to(px, py, human=True)
                        time.sleep(random.uniform(0.08, 0.25))
            time.sleep(random.uniform(0.15, 0.60))
        except Exception:
            pass

    def _human_approach_slider(self, page: Page, box: Dict):
        viewport = page.viewport_size or {'width': 1280, 'height': 720}
        start_x = random.randint(50, viewport['width'] - 50)
        start_y = random.randint(50, viewport['height'] - 50)

        self.human.move_to(start_x, start_y, human=True)

        target_x = box['x'] + box['width'] / 2
        target_y = box['y'] + box['height'] / 2

        for _ in range(random.randint(2, 4)):
            wx = random.randint(int(target_x - 80), int(target_x + 80))
            wy = random.randint(int(target_y - 40), int(target_y + 40))
            self.human.move_to(wx, wy, human=True)
            time.sleep(random.uniform(0.05, 0.15))

        self.human.move_to(target_x, target_y, human=True, hesitate=True)

    def _human_drag(self, page: Page, box: Dict, distance: int):
        start_x = box['x'] + box['width'] / 2
        start_y = box['y'] + box['height'] / 2

        num_points = random.randint(40, 60)

        roll = random.random()
        if roll < 0.70:
            overshoot = random.uniform(3.0, 9.0)
            target_distance = distance + overshoot
            correction_kind = 'pullback'
        elif roll < 0.90:
            undershoot = random.uniform(2.0, 6.0)
            target_distance = distance - undershoot
            correction_kind = 'creep'
        else:
            overshoot = 0.0
            target_distance = distance
            correction_kind = 'none'

        drag_points: List[Tuple[float, float]] = []

        for i in range(num_points + 1):
            t = i / num_points
            eased = 1 - (1 - t) ** 3
            x = start_x + eased * target_distance
            wobble = math.sin(t * math.pi) * random.uniform(0.4, 1.2)
            y = start_y + wobble + random.uniform(-0.3, 0.3)
            drag_points.append((x, y))

        if correction_kind == 'pullback':
            drag_points.append((start_x + target_distance, start_y))
            correction_steps = random.randint(10, 18)
            for i in range(1, correction_steps + 1):
                t = i / correction_steps
                eased = t * t * (3 - 2 * t)
                x = start_x + target_distance - (overshoot * eased)
                y = start_y + random.uniform(-0.4, 0.4)
                drag_points.append((x, y))
        elif correction_kind == 'creep':
            correction_steps = random.randint(8, 14)
            for i in range(1, correction_steps + 1):
                t = i / correction_steps
                eased = t * t * (3 - 2 * t)
                x = start_x + target_distance + (undershoot * eased)
                y = start_y + random.uniform(-0.4, 0.4)
                drag_points.append((x, y))

        total_time = random.uniform(0.55, 1.05)
        step_time = total_time / num_points

        for i, (x, y) in enumerate(drag_points):
            if i < num_points:
                delay = step_time * random.uniform(0.8, 1.2)
            elif i == num_points:
                delay = random.uniform(0.05, 0.15) if correction_kind != 'none' else 0.0
            else:
                delay = random.uniform(0.02, 0.05)

            page.mouse.move(x, y)
            if delay > 0:
                time.sleep(delay)

    def _get_captcha_data(self, page: Page) -> Optional[Dict]:
        try:
            # wait for the puzzle piece image to finish loading (real width, stable)
            try:
                page.wait_for_function(
                    """() => {
                        const p = document.querySelector('#aliyunCaptcha-puzzle');
                        const bg = document.querySelector('#aliyunCaptcha-img');
                        if (!p || !bg) return false;
                        const pw = p.naturalWidth || p.width || 0;
                        const bw = bg.naturalWidth || bg.width || 0;
                        return pw > 20 && bw > 100 && p.src.startsWith('data:image') && bg.src.startsWith('data:image');
                    }""",
                    timeout=8000,
                )
            except Exception:
                pass
            data = page.evaluate("""
                async () => {
                    const bg = document.querySelector('#aliyunCaptcha-img');
                    const puzzle = document.querySelector('#aliyunCaptcha-puzzle');
                    if (!bg || !puzzle) return null;
                    if (!bg.src || !puzzle.src) return null;

                    const bgRect = bg.getBoundingClientRect();
                    const puzzleRect = puzzle.getBoundingClientRect();

                    const fetchImage = async (url) => {
                        if (url.startsWith('data:image')) return url;
                        try {
                            const resp = await fetch(url);
                            const blob = await resp.blob();
                            return await new Promise((resolve) => {
                                const reader = new FileReader();
                                reader.onloadend = () => resolve(reader.result);
                                reader.readAsDataURL(blob);
                            });
                        } catch (e) { return null; }
                    };

                    const bg_b64 = await fetchImage(bg.src);
                    const shadow_b64 = await fetchImage(puzzle.src);
                    if (!bg_b64 || !shadow_b64) return null;

                    return {
                        bg_b64: bg_b64,
                        shadow_b64: shadow_b64,
                        bg_css_width: bgRect.width,
                        bg_natural_width: bg.naturalWidth || bg.width || 300,
                        puzzle_css_width: puzzleRect.width,
                        puzzle_natural_width: puzzle.naturalWidth || puzzle.width || 60,
                        puzzle_left_css: puzzleRect.left - bgRect.left
                    };
                }
            """)

            if not data:
                return None

            for _ in range(10):
                if (data.get('bg_natural_width', 0) > 50
                        and data.get('puzzle_natural_width', 0) > 10):
                    break
                time.sleep(0.1)
                dims = page.evaluate("""
                    () => {
                        const bg = document.querySelector('#aliyunCaptcha-img');
                        const puzzle = document.querySelector('#aliyunCaptcha-puzzle');
                        if (!bg || !puzzle) return null;
                        const bgRect = bg.getBoundingClientRect();
                        const puzzleRect = puzzle.getBoundingClientRect();
                        return {
                            bg_css_width: bgRect.width,
                            bg_natural_width: bg.naturalWidth || bg.width || 300,
                            puzzle_css_width: puzzleRect.width,
                            puzzle_natural_width: puzzle.naturalWidth || puzzle.width || 60,
                            puzzle_left_css: puzzleRect.left - bgRect.left
                        };
                    }
                """)
                if not dims:
                    return None
                data.update(dims)

            def get_bytes(b64):
                if not b64:
                    return None
                if b64.startswith('data:image'):
                    return base64.b64decode(b64.split(',')[1])
                return base64.b64decode(b64)

            bg_bytes = get_bytes(data['bg_b64'])
            shadow_bytes = get_bytes(data['shadow_b64'])
            if not shadow_bytes or not bg_bytes:
                return None

            return {
                'shadow_bytes': shadow_bytes,
                'bg_bytes': bg_bytes,
                'bg_css_width': data['bg_css_width'],
                'bg_natural_width': data['bg_natural_width'],
                'puzzle_css_width': data['puzzle_css_width'],
                'puzzle_natural_width': data['puzzle_natural_width'],
                'puzzle_left_css': data['puzzle_left_css'],
            }
        except Exception as e:
            self.logger.debug(f"Error getting captcha data: {e}")
            return None

    def _calculate_distance_v2(self, captcha_data: Dict) -> Optional[VoteResult]:
        try:
            calc = DistanceCalculator(
                ocr=self._get_ocr,   # lazy: ddddocr imported only if a method needs it
                logger=self.logger,
                config=self.config,
            )
            return calc.calculate(captcha_data)
        except Exception as e:
            self.logger.error(f"Distance calculation error: {e}")
            return None

    def _calculate_human_distance(self, captcha_data: Dict) -> Optional[float]:
        vote = self._calculate_distance_v2(captcha_data)
        return vote.distance if vote else None

    def _record_outcome(self, distance: float, confidence: float, success: bool):
        self._attempt_history.append((distance, confidence, success))
        if len(self._attempt_history) > 10:
            self._attempt_history.pop(0)

        if not self.config.get('enable_adaptive_offset', True):
            return

        if success:
            if self._adaptive_offset:
                self.logger.info(f"    Adaptive offset cleared (was {self._adaptive_offset:+.2f}px)")
            self._adaptive_offset = 0.0
            return

        offsets = self.config.get('calibration_offsets', [-4, -2, 0, 2, 4])
        recent_fails = 0
        for _, _, ok in reversed(self._attempt_history):
            if ok:
                break
            recent_fails += 1

        if recent_fails >= 2:
            idx = (recent_fails - 2) % len(offsets)
            new_offset = float(offsets[idx])
            if new_offset != self._adaptive_offset:
                self._adaptive_offset = new_offset
                self.logger.info(f"    Adaptive offset -> {self._adaptive_offset:+.2f}px "
                                 f"(consecutive fails: {recent_fails})")

    def _check_result(self, page: Page) -> str:
        # The app submits /auths/signup ~15s AFTER the slide (observed: slide at
        # 09:42:36 -> POST at 09:42:53) and then navigates to /auth/verify. The old
        # 12s window gave up before any signal appeared while the server had in fact
        # accepted the captcha. Poll longer and accept the post-success signals.
        max_polls = self.config.get('result_poll_count', 120)
        gone_since = None

        for poll in range(max_polls):
            try:
                state = page.evaluate("""
                    () => {
                        // Explicit FAILURE first.
                        if (document.querySelector('.aliyunCaptcha-verify-error')) return 'failed:class';
                        if (document.body && /verification failed/i.test(document.body.innerText || ''))
                            return 'failed:text';

                        // Explicit SUCCESS signals.
                        if (window.__signupFired) return 'success:signup';
                        if (document.querySelector('.aliyunCaptcha-verify-success')) return 'success:class';
                        const param = document.querySelector('#aliyunCaptcha-verify-param');
                        if (param && param.value && param.value.length > 20) return 'success:param';
                        if (window.__captchaParam || window.__verifyParam) return 'success:hook';
                        const path = window.location.pathname || '';
                        if (path.startsWith('/auth/verify')) return 'success:verifypage';
                        if (document.body && /verify your email/i.test(document.body.innerText || ''))
                            return 'success:verifytext';

                        // The whole widget disappears on success. Report presence so the
                        // caller can require it to STAY gone (a failed slide reloads the
                        // puzzle within ~1s, so gone-and-stays-gone means solved).
                        const present = !!(document.querySelector('#aliyunCaptcha-sliding-slider') ||
                                           document.querySelector('#aliyunCaptcha-popup') ||
                                           document.querySelector('[id^="aliyunCaptcha"]'));
                        return present ? 'pending:widget' : 'pending:gone';
                    }
                """)
                if state and state.startswith('success'):
                    self.logger.info(f"    Success detected (poll {poll}, {state.split(':')[1]})")
                    return 'success'
                if state and state.startswith('failed'):
                    self.logger.info(f"    Failed detected (poll {poll}, {state.split(':')[1]})")
                    return 'failed'
                if state == 'pending:gone':
                    if gone_since is None:
                        gone_since = time.time()
                    elif time.time() - gone_since >= 4.0:
                        self.logger.info(
                            f"    Success detected (widget gone {time.time() - gone_since:.1f}s)")
                        return 'success'
                else:
                    gone_since = None
            except Exception:
                pass
            time.sleep(0.2)

        self.logger.warning(f"    Result detection timed out after {max_polls * 0.2:.0f}s")
        try:
            dom_state = page.evaluate("""
                () => {
                    const result = {};
                    result.has_success_class = !!document.querySelector('.aliyunCaptcha-verify-success');
                    result.has_error_class = !!document.querySelector('.aliyunCaptcha-verify-error');
                    result.has_slider = !!document.querySelector('#aliyunCaptcha-sliding-slider');
                    result.has_popup = !!document.querySelector('#aliyunCaptcha-popup');
                    const param = document.querySelector('#aliyunCaptcha-verify-param');
                    result.verify_param_len = param ? param.value.length : 0;
                    result.url = window.location.href;
                    const aliyunEls = document.querySelectorAll('[class*="aliyun"], [id*="aliyun"]');
                    result.aliyun_elements = Array.from(aliyunEls).slice(0, 15).map(e => ({
                        tag: e.tagName,
                        id: e.id,
                        class: e.className,
                        visible: e.offsetParent !== null
                    }));
                    return result;
                }
            """)
            self.logger.info(f"    DOM state: {dom_state}")
        except Exception:
            pass

        return 'failed'

    def _on_success(self, page: Page) -> Optional[str]:
        self.logger.info("  Captcha solved!")
        time.sleep(0.5)
        try:
            t = page.evaluate(
                "() => { const e = document.querySelector('#aliyunCaptcha-verify-param'); return e?.value; }"
            )
            if t and len(t) > 20:
                self.logger.info(f"    Token: {t[:50]}...")
                return t
        except Exception:
            pass
        return 'success'
