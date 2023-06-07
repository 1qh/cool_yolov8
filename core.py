import json
import time
from dataclasses import asdict, dataclass, field
from subprocess import check_output

import cv2
import numpy as np
import streamlit as st
import yolov5
from dacite import from_dict
from PIL import Image
from streamlit import sidebar as sb
from streamlit_drawable_canvas import st_canvas
from supervision import (
    BoxAnnotator,
    Color,
    ColorPalette,
    Detections,
    LineZone,
    LineZoneAnnotator,
    MaskAnnotator,
    Point,
    PolygonZone,
    PolygonZoneAnnotator,
    crop,
    draw_text,
    get_polygon_center,
)
from ultralytics import RTDETR, YOLO

from color import colors, colors_rgb


def cvt(f):
    return cv2.cvtColor(f, cv2.COLOR_BGR2RGB)


def maxcam():
    reso = (
        check_output(
            "v4l2-ctl -d /dev/video0 --list-formats-ext | grep Size: | tail -1 | awk '{print $NF}'",
            shell=True,
        )
        .decode()
        .split('x')
    )
    width, height = [int(i) for i in reso] if len(reso) == 2 else (640, 480)
    return width, height


def plur(n, s):
    return f"\n- {n} {s}{'s'[:n^1]}" if n else ''


def rgb2hex(rgb):
    r, g, b = rgb
    return f'#{r:02x}{g:02x}{b:02x}'


def rgb2ycc(rgb):
    rgb = rgb / 255.0
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331364 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return np.stack([y, cb, cr], axis=-1)


def closest(rgb, ycc_colors):
    return np.argmin(np.sum((ycc_colors - rgb2ycc(rgb[np.newaxis])) ** 2, axis=1))


def avg_rgb(f):
    return cv2.kmeans(
        cvt(f.reshape(-1, 3).astype(np.float32)),
        1,
        None,
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0),
        10,
        cv2.KMEANS_RANDOM_CENTERS,
    )[2][0].astype(np.int32)


def mycanvas(stroke, width, height, mode, bg, key):
    return st_canvas(
        stroke_width=2,
        fill_color='#ffffff55',
        stroke_color=stroke,
        width=width,
        height=height,
        drawing_mode=mode,
        background_image=bg,
        key=key,
    )


ycc_colors = rgb2ycc(colors_rgb)
colors_rgb = [tuple(map(int, i)) for i in colors_rgb]


@dataclass
class Display:
    fps: bool = True
    predict_color: bool = False
    box: bool = True
    skip_label: bool = False
    mask: bool = True
    mask_opacity: float = 0.5
    area: bool = True


@dataclass
class Tweak:
    thickness: int = 1
    text_scale: float = 0.5
    text_offset: int = 1
    text_padding: int = 2
    text_color: str = '#000000'


@dataclass
class Draw:
    lines: list = field(default_factory=list)
    zones: list = field(default_factory=list)

    def __str__(self) -> str:
        return plur(len(self.lines), 'line') + plur(len(self.zones), 'zone')

    def __len__(self) -> int:
        return len(self.lines) + len(self.zones)

    @classmethod
    def from_canvas(cls, d: list):
        return cls(
            lines=[
                (
                    (i['left'] + i['x1'], i['top'] + i['y1']),
                    (i['left'] + i['x2'], i['top'] + i['y2']),
                )
                for i in d
                if i['type'] == 'line'
            ],
            zones=[
                [[x[1], x[2]] for x in k]
                for k in [j[:-1] for j in [i['path'] for i in d if i['type'] == 'path']]
            ]
            + [
                [
                    [i['left'], i['top']],
                    [i['left'] + i['width'], i['top']],
                    [i['left'] + i['width'], i['top'] + i['height']],
                    [i['left'], i['top'] + i['height']],
                ]
                for i in d
                if i['type'] == 'rect'
            ],
        )


@dataclass
class ModelInfo:
    path: str = 'yolov8n.pt'
    classes: list[int] = field(default_factory=list)
    ver: str = 'v8'
    task: str = 'detect'
    conf: float = 0.25
    tracker: str | None = None


class Model:
    def __init__(
        self,
        info: ModelInfo = ModelInfo(),
    ):
        self.classes = info.classes
        self.conf = info.conf
        self.tracker = info.tracker

        path = info.path
        ver = info.ver

        self.legacy = ver == 'v5'

        if ver == 'rtdetr':
            self.model = RTDETR(path)
            self.names = []  # not available
        else:
            self.model = YOLO(path) if not self.legacy else yolov5.load(path)
            self.names = self.model.names

        if self.legacy:
            self.model.classes = self.classes
            self.model.conf = self.conf

        self.info = info

    def __call__(self, source):
        return (
            self.model.predict(
                source,
                classes=self.classes,
                conf=self.conf,
                retina_masks=True,
            )
            if self.tracker is None
            else self.model.track(
                source,
                classes=self.classes,
                conf=self.conf,
                retina_masks=True,
                tracker=f'{self.tracker}.yaml',
            )
        )

    def det(self, f):
        if self.legacy:
            return Detections.from_yolov5(self.model(f)), cvt(f)

        res = self(f)[0]
        if res.boxes is not None:
            det = Detections.from_yolov8(res)
            if res.boxes.id is not None:
                det.tracker_id = res.boxes.id.cpu().numpy().astype(int)
            return det, cvt(res.plot())

        return Detections.empty(), cvt(res.plot())


class Annotator:
    def __init__(
        self,
        model: Model,
        reso: tuple[int, int],
        draw: Draw = Draw(),
        display: Display = Display(),
        tweak: Tweak = Tweak(),
    ):
        self.model = model
        self.reso = reso
        self.draw = draw
        self.display = display
        self.tweak = tweak
        self.ls = [
            LineZone(start=Point(i[0][0], i[0][1]), end=Point(i[1][0], i[1][1]))
            for i in self.draw.lines
        ]
        self.zs = [
            PolygonZone(polygon=np.array(p), frame_resolution_wh=reso)
            for p in self.draw.zones
        ]
        self.text_color = Color.from_hex(tweak.text_color)
        self.line = LineZoneAnnotator(
            thickness=tweak.thickness,
            text_color=self.text_color,
            text_scale=tweak.text_scale,
            text_offset=tweak.text_offset,
            text_padding=tweak.text_padding,
        )
        self.box = BoxAnnotator(
            thickness=tweak.thickness,
            text_color=self.text_color,
            text_scale=tweak.text_scale,
            text_padding=tweak.text_padding,
        )
        self.zones = [
            PolygonZoneAnnotator(
                thickness=tweak.thickness,
                text_color=self.text_color,
                text_scale=tweak.text_scale,
                text_padding=tweak.text_padding,
                zone=z,
                color=ColorPalette.default().by_idx(i),
            )
            for i, z in enumerate(self.zs)
        ]
        self.mask = MaskAnnotator()

    def __dict__(self):
        return {
            'model': asdict(self.model.info),
            'draw': asdict(self.draw),
            'display': asdict(self.display),
            'tweak': asdict(self.tweak),
        }

    def dump(self, path: str):
        with open(path, 'w') as f:
            json.dump(self.__dict__(), f, indent=2)

    @classmethod
    def load(cls, path: str, reso: tuple[int, int]):
        d = json.load(open(path))
        return cls(
            model=Model(from_dict(ModelInfo, d['model'])),
            reso=reso,
            display=from_dict(Display, d['display']),
            tweak=from_dict(Tweak, d['tweak']),
            draw=from_dict(Draw, d['draw']),
        )

    def __call__(self, f: np.ndarray):
        begin = time.time()
        dp = self.display
        tw = self.tweak
        names = self.model.names
        det, res = self.model.det(f)
        xyxy = det.xyxy.astype(int)

        if dp.predict_color:
            naive = False
            centers = (xyxy[:, [0, 1]] + xyxy[:, [2, 3]]) // 2

            for i in range(xyxy.shape[0]):
                x = centers[i][0]
                y = centers[i][1]
                bb = xyxy[i]

                # for shirt color of person
                # w = bb[2] - bb[0]
                # h = bb[3] - bb[1]
                # cropped = f[
                #     bb[1] : bb[3] - int(h * 0.4),
                #     bb[0] + int(w * 0.2) : bb[2] - int(w * 0.2),
                # ]

                cropped = crop(f, bb)
                rgb = f[y, x] if naive else avg_rgb(cropped)
                predict = closest(rgb, ycc_colors)
                r, g, b = colors_rgb[predict]
                draw_text(
                    scene=f,
                    text=colors[predict],
                    text_anchor=Point(x=x, y=y + 20),
                    text_color=Color(255 - r, 255 - g, 255 - b),
                    text_scale=tw.text_scale,
                    text_padding=tw.text_padding,
                    background_color=Color(r, g, b),
                )
        if dp.box:
            f = self.box.annotate(
                scene=f,
                detections=det,
                labels=[
                    f'{conf:0.2f} {names[cl] if len(names) else cl}'
                    + (f' {track_id}' if track_id else '')
                    for _, _, conf, cl, track_id in det
                ],
                skip_label=dp.skip_label,
            )
        if dp.mask:
            f = self.mask.annotate(
                scene=f,
                detections=det,
                opacity=dp.mask_opacity,
            )
        if dp.area:
            for t, a in zip(det.area, xyxy.astype(int)):
                draw_text(
                    scene=f,
                    text=f'{int(t)}',
                    text_anchor=Point(x=(a[0] + a[2]) // 2, y=(a[1] + a[3]) // 2),
                    text_color=self.text_color,
                    text_scale=tw.text_scale,
                    text_padding=tw.text_padding,
                )
        for l in self.ls:
            l.trigger(det)
            self.line.annotate(frame=f, line_counter=l)

        for z, zone in zip(self.zs, self.zones):
            z.trigger(det)
            f = zone.annotate(f)

        if dp.fps:
            fps = 1 / (time.time() - begin)
            draw_text(
                scene=f,
                text=f'{fps:.1f}',
                text_anchor=Point(x=50, y=20),
                text_color=self.text_color,
                text_scale=tw.text_scale * 2,
                text_padding=tw.text_padding,
            )
        return f, res

    def update(self, f: np.ndarray):
        scale = f.shape[0] / self.reso[1]
        self.ls = [
            LineZone(
                start=Point(i[0][0] * scale, i[0][1] * scale),
                end=Point(i[1][0] * scale, i[1][1] * scale),
            )
            for i in self.draw.lines
        ]
        origin_zs = [
            PolygonZone(polygon=np.array(p), frame_resolution_wh=self.reso)
            for p in self.draw.zones
        ]
        self.zs = [
            PolygonZone(
                polygon=(z.polygon * scale).astype(int),
                frame_resolution_wh=(f.shape[1], f.shape[0]),
            )
            for z in origin_zs
        ]
        for i, z in enumerate(self.zs):
            self.zones[i].zone = z
            self.zones[i].center = get_polygon_center(polygon=z.polygon)

    @classmethod
    def ui(
        cls,
        info: ModelInfo,
        reso: tuple[int, int],
        background: Image.Image | None,
    ):
        width, height = reso
        c1, c2, c3, c4 = st.columns(4)
        mode = c1.selectbox(
            'Draw',
            ('line', 'rect', 'polygon'),
            label_visibility='collapsed',
        )
        bg = background if c4.checkbox('Background', value=True) else None
        stroke, key = ('#fff', 'e') if bg is None else ('#000', 'f')
        canvas = mycanvas(stroke, width, height, mode, bg, key)

        draw = Draw()

        if canvas.json_data is not None:
            draw = Draw.from_canvas(canvas.json_data['objects'])
            c2.markdown(draw)

        if canvas.image_data is not None and len(draw) > 0:
            if c3.button('Export canvas image'):
                Image.alpha_composite(
                    bg.convert('RGBA'),
                    Image.fromarray(canvas.image_data),
                ).save('canvas.png')

        c1, c2 = sb.columns(2)
        c3, c4 = sb.columns(2)
        c5, c6 = sb.columns(2)

        display = Display(
            fps=c1.checkbox('Show FPS', value=True),
            predict_color=c2.checkbox('Predict color'),
            box=c3.checkbox('Box', value=True),
            skip_label=not c4.checkbox('Label', value=True),
            mask=c5.checkbox('Mask', value=True) if info.task == 'segment' else False,
            mask_opacity=sb.slider('Opacity', 0.0, 1.0, 0.5)
            if info.task == 'segment'
            else 0.0,
            area=c6.checkbox('Area', value=True),
        )
        if display.predict_color:
            for color, rgb in zip(colors, colors_rgb):
                sb.color_picker(f'{color}', value=rgb2hex(rgb))

        tweak = Tweak(
            thickness=sb.slider('Thickness', 0, 10, 1),
            text_scale=sb.slider('Text size', 0.0, 2.0, 0.5),
            text_offset=sb.slider('Text offset', 0, 10, 1) if len(draw.lines) else 0,
            text_padding=sb.slider('Text padding', 0, 10, 2),
            text_color=sb.color_picker('Text color', '#000000'),
        )

        return cls(
            model=Model(info),
            reso=reso,
            display=display,
            tweak=tweak,
            draw=draw,
        )
