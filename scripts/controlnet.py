import gc
import inspect
import os
from collections import OrderedDict
from copy import copy
from typing import Union, Dict, Optional, List
import importlib

import numpy
import torch

import modules.scripts as scripts
from modules import shared, devices, script_callbacks, processing, masking, images
import gradio as gr
import numpy as np

from einops import rearrange
from scripts import global_state, hook, external_code, processor, batch_hijack, controlnet_version
importlib.reload(processor)
importlib.reload(global_state)
importlib.reload(hook)
importlib.reload(external_code)
importlib.reload(batch_hijack)
from scripts.cldm import PlugableControlModel
from scripts.processor import *
from scripts.adapter import PlugableAdapter
from scripts.utils import load_state_dict
from scripts.hook import ControlParams, UnetHook
from modules.processing import StableDiffusionProcessingImg2Img, StableDiffusionProcessingTxt2Img
from modules.images import save_image
from modules.ui_components import FormRow
import cv2
from pathlib import Path
from PIL import Image, ImageFilter, ImageOps
from scripts.lvminthin import lvmin_thin, nake_nms
from torchvision.transforms import Resize, InterpolationMode, CenterCrop, Compose

gradio_compat = True
try:
    from distutils.version import LooseVersion
    from importlib_metadata import version
    if LooseVersion(version("gradio")) < LooseVersion("3.10"):
        gradio_compat = False
except ImportError:
    pass

# svgsupports
svgsupport = False
try:
    import io
    import base64
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPM
    svgsupport = True
except ImportError:
    pass

# Note: Change symbol hints mapping in `javascript/hints.js` when you change the symbol values.
refresh_symbol = '\U0001f504'       # 🔄
switch_values_symbol = '\U000021C5' # ⇅
camera_symbol = '\U0001F4F7'        # 📷
reverse_symbol = '\U000021C4'       # ⇄
tossup_symbol = '\u2934'
trigger_symbol = '\U0001F4A5'  # 💥
open_symbol = '\U0001F4DD'  # 📝

webcam_enabled = False
webcam_mirrored = False

global_batch_input_dir = gr.Textbox(
    label='Controlnet input directory',
    placeholder='Leave empty to use input directory',
    **shared.hide_dirs,
    elem_id='controlnet_batch_input_dir')
img2img_batch_input_dir = None
img2img_batch_input_dir_callbacks = []
img2img_batch_output_dir = None
img2img_batch_output_dir_callbacks = []
generate_buttons = {}

txt2img_submit_button = None
img2img_submit_button = None


class ToolButton(gr.Button, gr.components.FormComponent):
    """Small button with single emoji as text, fits inside gradio forms"""

    def __init__(self, **kwargs):
        super().__init__(variant="tool", elem_classes=['cnet-toolbutton'], **kwargs)

    def get_block_name(self):
        return "button"


def find_closest_lora_model_name(search: str):
    if not search:
        return None
    if search in global_state.cn_models:
        return search
    search = search.lower()
    if search in global_state.cn_models_names:
        return global_state.cn_models_names.get(search)
    applicable = [name for name in global_state.cn_models_names.keys()
                  if search in name.lower()]
    if not applicable:
        return None
    applicable = sorted(applicable, key=lambda name: len(name))
    return global_state.cn_models_names[applicable[0]]


def swap_img2img_pipeline(p: processing.StableDiffusionProcessingImg2Img):
    p.__class__ = processing.StableDiffusionProcessingTxt2Img
    dummy = processing.StableDiffusionProcessingTxt2Img()
    for k,v in dummy.__dict__.items():
        if hasattr(p, k):
            continue
        setattr(p, k, v)


global_state.update_cn_models()


def image_dict_from_any(image) -> Optional[Dict[str, np.ndarray]]:
    if image is None:
        return None

    if isinstance(image, (tuple, list)):
        image = {'image': image[0], 'mask': image[1]}
    elif not isinstance(image, dict):
        image = {'image': image, 'mask': None}
    else:  # type(image) is dict
        # copy to enable modifying the dict and prevent response serialization error
        image = dict(image)

    if isinstance(image['image'], str):
        if os.path.exists(image['image']):
            image['image'] = numpy.array(Image.open(image['image'])).astype('uint8')
        else:
            image['image'] = external_code.to_base64_nparray(image['image'])

    if isinstance(image['mask'], str):
        if os.path.exists(image['mask']):
            image['mask'] = numpy.array(Image.open(image['mask'])).astype('uint8')
        else:
            image['mask'] = external_code.to_base64_nparray(image['mask'])
    elif image['mask'] is None:
        image['mask'] = np.zeros_like(image['image'], dtype=np.uint8)

    return image


class UiControlNetUnit(external_code.ControlNetUnit):
    def __init__(
        self,
        input_mode: batch_hijack.InputMode = batch_hijack.InputMode.SIMPLE,
        batch_images: Optional[Union[str, List[external_code.InputImage]]] = None,
        output_dir: str = '',
        loopback: bool = False,
        *args, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.is_ui = True
        self.input_mode = input_mode
        self.batch_images = batch_images
        self.output_dir = output_dir
        self.loopback = loopback


class Script(scripts.Script):
    model_cache = OrderedDict()

    def __init__(self) -> None:
        super().__init__()
        self.latest_network = None
        self.preprocessor = global_state.cn_preprocessor_modules
        self.unloadable = global_state.cn_preprocessor_unloadable
        self.input_image = None
        self.latest_model_hash = ""
        self.txt2img_w_slider = gr.Slider()
        self.txt2img_h_slider = gr.Slider()
        self.img2img_w_slider = gr.Slider()
        self.img2img_h_slider = gr.Slider()
        self.enabled_units = []
        self.detected_map = []
        batch_hijack.instance.process_batch_callbacks.append(self.batch_tab_process)
        batch_hijack.instance.process_batch_each_callbacks.append(self.batch_tab_process_each)
        batch_hijack.instance.postprocess_batch_each_callbacks.insert(0, self.batch_tab_postprocess_each)
        batch_hijack.instance.postprocess_batch_callbacks.insert(0, self.batch_tab_postprocess)

    def title(self):
        return "ControlNet"

    def show(self, is_img2img):
        # if is_img2img:
            # return False
        return scripts.AlwaysVisible

    def after_component(self, component, **kwargs):
        if component.elem_id == "txt2img_width":
            self.txt2img_w_slider = component
            return self.txt2img_w_slider
        if component.elem_id == "txt2img_height":
            self.txt2img_h_slider = component
            return self.txt2img_h_slider
        if component.elem_id == "img2img_width":
            self.img2img_w_slider = component
            return self.img2img_w_slider
        if component.elem_id == "img2img_height":
            self.img2img_h_slider = component
            return self.img2img_h_slider

    def get_module_basename(self, module):
        return global_state.reverse_preprocessor_aliases.get(module, module)

    def get_threshold_block(self, proc):
        pass

    def get_default_ui_unit(self, is_ui=True):
        cls = UiControlNetUnit if is_ui else external_code.ControlNetUnit
        return cls(
            enabled=False,
            module="none",
            model="None",
            guess_mode=False,
        )

    def uigroup(self, tabname, is_img2img, elem_id_tabname):
        infotext_fields = []
        default_unit = self.get_default_ui_unit()
        with gr.Tabs():
            with gr.Tab(label='Single Image') as upload_tab:
                with gr.Row(equal_height=True):
                    input_image = gr.Image(source='upload', brush_radius=20, mirror_webcam=False, type='numpy', tool='sketch', elem_id=f'{elem_id_tabname}_{tabname}_input_image')
                    # Gradio's magic number. Only 242 works.
                    generated_image = gr.Image(label="Preprocessor Preview", visible=False, elem_id=f'{elem_id_tabname}_{tabname}_generated_image').style(height=242)

            with gr.Tab(label='Batch') as batch_tab:
                batch_image_dir = gr.Textbox(label='Input Directory', placeholder='Leave empty to use img2img batch controlnet input directory', elem_id=f'{elem_id_tabname}_{tabname}_batch_image_dir')

        with gr.Accordion(label='Open New Canvas', visible=False) as create_canvas:
            canvas_width = gr.Slider(label="New Canvas Width", minimum=256, maximum=1024, value=512, step=64)
            canvas_height = gr.Slider(label="New Canvas Height", minimum=256, maximum=1024, value=512, step=64)
            with gr.Row():
                canvas_create_button = gr.Button(value="Create New Canvas")
                canvas_cancel_button = gr.Button(value="Cancel")

        with gr.Row():
            gr.HTML(value='<p>Set the preprocessor to [invert] If your image has white background and black lines.</p>')
            open_new_canvas_button = ToolButton(value=open_symbol)
            webcam_enable = ToolButton(value=camera_symbol)
            webcam_mirror = ToolButton(value=reverse_symbol)
            send_dimen_button = ToolButton(value=tossup_symbol)

        open_new_canvas_button.click(lambda: gr.Accordion.update(visible=True), inputs=None, outputs=create_canvas)
        canvas_cancel_button.click(lambda: gr.Accordion.update(visible=False), inputs=None, outputs=create_canvas)

        with FormRow(elem_classes="checkboxes-row", variant="compact"):
            enabled = gr.Checkbox(label='Enable', value=default_unit.enabled)
            lowvram = gr.Checkbox(label='Low VRAM', value=default_unit.low_vram)
            guess_mode = gr.Checkbox(label='Guess Mode', value=default_unit.guess_mode, interactive=False, visible=False)
            # Guess mode will be removed soon after API is fixed.
            pixel_perfect = gr.Checkbox(label='Pixel Perfect', value=default_unit.pixel_perfect)
            preprocessor_preview = gr.Checkbox(label='Allow Preview', value=False)

        # infotext_fields.append((enabled, "ControlNet Enabled"))

        def send_dimensions(image):
            def closesteight(num):
                rem = num % 8
                if rem <= 4:
                    return round(num - rem)
                else:
                    return round(num + (8 - rem))
            if(image):
                interm = np.asarray(image.get('image'))
                return closesteight(interm.shape[1]), closesteight(interm.shape[0])
            else:
                return gr.Slider.update(), gr.Slider.update()

        def webcam_toggle():
            global webcam_enabled
            webcam_enabled = not webcam_enabled
            return {"value": None, "source": "webcam" if webcam_enabled else "upload", "__type__": "update"}

        def webcam_mirror_toggle():
            global webcam_mirrored
            webcam_mirrored = not webcam_mirrored
            return {"mirror_webcam": webcam_mirrored, "__type__": "update"}

        webcam_enable.click(fn=webcam_toggle, inputs=None, outputs=input_image)
        webcam_mirror.click(fn=webcam_mirror_toggle, inputs=None, outputs=input_image)

        def refresh_all_models(*inputs):
            global_state.update_cn_models()

            dd = inputs[0]
            selected = dd if dd in global_state.cn_models else "None"
            return gr.Dropdown.update(value=selected, choices=list(global_state.cn_models.keys()))

        with gr.Row():
            module = gr.Dropdown(global_state.ui_preprocessor_keys, label=f"Preprocessor", value=default_unit.module)
            trigger_preprocessor = ToolButton(value=trigger_symbol, visible=False)
            model = gr.Dropdown(list(global_state.cn_models.keys()), label=f"Model", value=default_unit.model)
            refresh_models = ToolButton(value=refresh_symbol)
            refresh_models.click(refresh_all_models, model, model)

        with gr.Row():
            weight = gr.Slider(label=f"Control Weight", value=default_unit.weight, minimum=0.0, maximum=2.0, step=.05)
            guidance_start = gr.Slider(label="Starting Control Step", value=default_unit.guidance_start, minimum=0.0, maximum=1.0, interactive=True)
            guidance_end = gr.Slider(label="Ending Control Step", value=default_unit.guidance_end, minimum=0.0, maximum=1.0, interactive=True)

        def build_sliders(module, pp):
            module = self.get_module_basename(module)
            if module == "canny":
                return [
                    gr.update(label="Preprocessor resolution", value=512, minimum=64, maximum=2048, step=1, visible=not pp, interactive=not pp),
                    gr.update(label="Canny low threshold", minimum=1, maximum=255, value=100, step=1, visible=True, interactive=True),
                    gr.update(label="Canny high threshold", minimum=1, maximum=255, value=200, step=1, visible=True, interactive=True),
                    gr.update(visible=True)
                ]
            elif module == "mlsd": #Hough
                return [
                    gr.update(label="Preprocessor Resolution", minimum=64, maximum=2048, value=512, step=1, visible=not pp, interactive=not pp),
                    gr.update(label="Hough value threshold (MLSD)", minimum=0.01, maximum=2.0, value=0.1, step=0.01, visible=True, interactive=True),
                    gr.update(label="Hough distance threshold (MLSD)", minimum=0.01, maximum=20.0, value=0.1, step=0.01, visible=True, interactive=True),
                    gr.update(visible=True)
                ]
            elif module in ["hed", "scribble_hed", "hed_safe"]:
                return [
                    gr.update(label="Preprocessor Resolution", minimum=64, maximum=2048, value=512, step=1, visible=not pp, interactive=not pp),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=True)
                ]
            elif module in ["openpose", "openpose_full", "segmentation"]:
                return [
                    gr.update(label="Preprocessor Resolution", minimum=64, maximum=2048, value=512, step=1, visible=not pp, interactive=not pp),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=True)
                ]
            elif module == "depth":
                return [
                    gr.update(label="Preprocessor Resolution", minimum=64, maximum=2048, value=512, step=1, visible=not pp, interactive=not pp),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=True)
                ]
            elif module in ["depth_leres", "depth_leres_boost"]:
                return [
                    gr.update(label="Preprocessor Resolution", minimum=64, maximum=2048, value=512, step=1, visible=not pp, interactive=not pp),
                    gr.update(label="Remove Near %", value=0, minimum=0, maximum=100, step=0.1, visible=True, interactive=True),
                    gr.update(label="Remove Background %", value=0, minimum=0, maximum=100, step=0.1, visible=True, interactive=True),
                    gr.update(visible=True)
                ]
            elif module == "normal_map":
                return [
                    gr.update(label="Preprocessor Resolution", minimum=64, maximum=2048, value=512, step=1, visible=not pp, interactive=not pp),
                    gr.update(label="Normal background threshold", minimum=0.0, maximum=1.0, value=0.4, step=0.01, visible=True, interactive=True),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=True)
                ]
            elif module == "threshold":
                return [
                    gr.update(label="Preprocessor resolution", value=512, minimum=64, maximum=2048, step=1, visible=not pp, interactive=not pp),
                    gr.update(label="Binarization Threshold", minimum=0, maximum=255, value=127, step=1, visible=True, interactive=True),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=True)
                ]
            elif module == "scribble_xdog":
                return [
                    gr.update(label="Preprocessor resolution", value=512, minimum=64, maximum=2048, step=1, visible=not pp, interactive=not pp),
                    gr.update(label="XDoG Threshold", minimum=1, maximum=64, value=32, step=1, visible=True, interactive=True),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=True)
                ]
            elif module == "tile_gaussian":
                return [
                    gr.update(visible=False, interactive=False),
                    gr.update(label="Down Sampling Rate", value=1.0, minimum=1.0, maximum=8.0, step=0.01, visible=True, interactive=True),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=True)
                ]
            elif module == "color":
                return [
                    gr.update(label="Preprocessor Resolution", value=512, minimum=64, maximum=2048, step=8, visible=not pp, interactive=not pp),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=True)
                ]
            elif module == "mediapipe_face":
                return [
                    gr.update(label="Preprocessor Resolution", value=512, minimum=64, maximum=2048, step=8, visible=not pp, interactive=not pp),
                    gr.update(label="Max Faces", value=1, minimum=1, maximum=10, step=1, visible=True, interactive=True),
                    gr.update(label="Min Face Confidence", value=0.5, minimum=0.01, maximum=1.0, step=0.01, visible=True, interactive=True),
                    gr.update(visible=True)
                ]
            elif module == "none" or "inpaint" in module:
                return [
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=False)
                ]
            else:
                return [
                    gr.update(label="Preprocessor resolution", value=512, minimum=64, maximum=2048, step=1, visible=not pp, interactive=not pp),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=False, interactive=False),
                    gr.update(visible=True)
                ]

        # advanced options
        with gr.Column(visible=False) as advanced:
            processor_res = gr.Slider(label="Preprocessor resolution", value=default_unit.processor_res, minimum=64, maximum=2048, visible=False, interactive=False)
            threshold_a = gr.Slider(label="Threshold A", value=default_unit.threshold_a, minimum=64, maximum=1024, visible=False, interactive=False)
            threshold_b = gr.Slider(label="Threshold B", value=default_unit.threshold_b, minimum=64, maximum=1024, visible=False, interactive=False)

        if gradio_compat:
            module.change(build_sliders, inputs=[module, pixel_perfect], outputs=[processor_res, threshold_a, threshold_b, advanced])
            pixel_perfect.change(build_sliders, inputs=[module, pixel_perfect], outputs=[processor_res, threshold_a, threshold_b, advanced])

        # infotext_fields.extend((module, model, weight))

        def svgPreprocess(inputs):
            if (inputs):
                if (inputs['image'].startswith("data:image/svg+xml;base64,") and svgsupport):
                    svg_data = base64.b64decode(inputs['image'].replace('data:image/svg+xml;base64,',''))
                    drawing = svg2rlg(io.BytesIO(svg_data))
                    png_data = renderPM.drawToString(drawing, fmt='PNG')
                    encoded_string = base64.b64encode(png_data)
                    base64_str = str(encoded_string, "utf-8")
                    base64_str = "data:image/png;base64,"+ base64_str
                    inputs['image'] = base64_str
                return input_image.orgpreprocess(inputs)
            return None

        def run_annotator(image, module, pres, pthr_a, pthr_b):
            if image is None:
                return gr.update(value=None, visible=True)

            img = HWC3(image['image'])
            if not ((image['mask'][:, :, 0] == 0).all() or (image['mask'][:, :, 0] == 255).all()):
                img = HWC3(image['mask'][:, :, 0])

            if 'inpaint' in module:
                color = HWC3(image['image'])
                alpha = image['mask'][:, :, 0:1]
                img = np.concatenate([color, alpha], axis=2)

            module = self.get_module_basename(module)
            preprocessor = self.preprocessor[module]

            result, is_image = preprocessor(img, res=pres, thr_a=pthr_a, thr_b=pthr_b)

            if is_image:
                if result.ndim == 3 and result.shape[2] == 4:
                    inpaint_mask = result[:, :, 3]
                    result = result[:, :, 0:3]
                    result[inpaint_mask > 127] = 0
                return gr.update(value=result, visible=True, interactive=False)

            return gr.update(value=None, visible=True)

        def shift_preview(is_on):
            if is_on:
                return gr.update(visible=True), gr.update(value=None, visible=True)
            else:
                return gr.update(visible=False), gr.update(visible=False)

        preprocessor_preview.change(fn=shift_preview, inputs=[preprocessor_preview], outputs=[trigger_preprocessor, generated_image])
        trigger_preprocessor.click(fn=run_annotator, inputs=[input_image, module, processor_res, threshold_a, threshold_b], outputs=[generated_image])

        if is_img2img:
            send_dimen_button.click(fn=send_dimensions, inputs=[input_image], outputs=[self.img2img_w_slider, self.img2img_h_slider])
        else:
            send_dimen_button.click(fn=send_dimensions, inputs=[input_image], outputs=[self.txt2img_w_slider, self.txt2img_h_slider])

        control_mode = gr.Radio(choices=[e.value for e in external_code.ControlMode], value=default_unit.control_mode.value, label="Control Mode (Guess Mode)")

        resize_mode = gr.Radio(choices=[e.value for e in external_code.ResizeMode], value=default_unit.resize_mode.value, label="Resize Mode")

        loopback = gr.Checkbox(label='[Loopback] Automatically send generated images to this ControlNet unit', value=default_unit.loopback)

        def fn_canvas(h, w):
            return np.zeros(shape=(h, w, 3), dtype=np.uint8) + 255, gr.Accordion.update(visible=False)

        canvas_create_button.click(fn=fn_canvas, inputs=[canvas_height, canvas_width], outputs=[input_image, create_canvas])

        input_mode = gr.State(batch_hijack.InputMode.SIMPLE)
        batch_image_dir_state = gr.State('')
        output_dir_state = gr.State('')
        unit_args = (input_mode, batch_image_dir_state, output_dir_state, loopback, enabled, module, model, weight, input_image, resize_mode, lowvram, processor_res, threshold_a, threshold_b, guidance_start, guidance_end, guess_mode, pixel_perfect, control_mode)
        self.register_modules(tabname, unit_args)

        input_image.orgpreprocess=input_image.preprocess
        input_image.preprocess=svgPreprocess

        unit = gr.State(default_unit)
        for comp in unit_args:
            event_subscribers = []
            if hasattr(comp, 'edit'):
                event_subscribers.append(comp.edit)
            elif hasattr(comp, 'click'):
                event_subscribers.append(comp.click)
            elif hasattr(comp, 'change'):
                event_subscribers.append(comp.change)

            if hasattr(comp, 'clear'):
                event_subscribers.append(comp.clear)

            for event_subscriber in event_subscribers:
                event_subscriber(fn=UiControlNetUnit, inputs=list(unit_args), outputs=unit)

        # keep input_mode in sync
        def ui_controlnet_unit_for_input_mode(input_mode, *args):
            args = list(args)
            args[0] = input_mode
            return input_mode, UiControlNetUnit(*args)

        for input_tab in (
            (upload_tab, batch_hijack.InputMode.SIMPLE),
            (batch_tab, batch_hijack.InputMode.BATCH)
        ):
            input_tab[0].select(fn=ui_controlnet_unit_for_input_mode, inputs=[gr.State(input_tab[1])] + list(unit_args), outputs=[input_mode, unit])

        def determine_batch_dir(batch_dir, fallback_dir, fallback_fallback_dir):
            if batch_dir:
                return batch_dir
            elif fallback_dir:
                return fallback_dir
            else:
                return fallback_fallback_dir

        # keep batch_dir in sync with global batch input textboxes
        global img2img_batch_input_dir, img2img_batch_input_dir_callbacks
        def subscribe_for_batch_dir():
            global global_batch_input_dir, img2img_batch_input_dir
            batch_dirs = [batch_image_dir, global_batch_input_dir, img2img_batch_input_dir]
            for batch_dir_comp in batch_dirs:
                subscriber = getattr(batch_dir_comp, 'blur', None)
                if subscriber is None: continue
                subscriber(
                    fn=determine_batch_dir,
                    inputs=batch_dirs,
                    outputs=[batch_image_dir_state],
                    queue=False,
                )

        if img2img_batch_input_dir is None:
            # we are too soon, subscribe later when available
            img2img_batch_input_dir_callbacks.append(subscribe_for_batch_dir)
        else:
            subscribe_for_batch_dir()

        # keep output_dir in sync with global batch output textbox
        global img2img_batch_output_dir, img2img_batch_output_dir_callbacks
        def subscribe_for_output_dir():
            global img2img_batch_output_dir
            img2img_batch_output_dir.blur(
                fn=lambda a: a,
                inputs=[img2img_batch_output_dir],
                outputs=[output_dir_state],
                queue=False,
            )

        if img2img_batch_input_dir is None:
            # we are too soon, subscribe later when available
            img2img_batch_output_dir_callbacks.append(subscribe_for_output_dir)
        else:
            subscribe_for_output_dir()

        if is_img2img:
            img2img_submit_button.click(fn=UiControlNetUnit, inputs=list(unit_args), outputs=unit, queue=False)
        else:
            txt2img_submit_button.click(fn=UiControlNetUnit, inputs=list(unit_args), outputs=unit, queue=False)

        return unit

    def ui(self, is_img2img):
        """this function should create gradio UI elements. See https://gradio.app/docs/#components
        The return value should be an array of all components that are used in processing.
        Values of those returned components will be passed to run() and process() functions.
        """
        self.infotext_fields = []
        self.paste_field_names = []
        controls = ()
        max_models = shared.opts.data.get("control_net_max_models_num", 1)
        elem_id_tabname = ("img2img" if is_img2img else "txt2img") + "_controlnet"
        with gr.Group(elem_id=elem_id_tabname):
            with gr.Accordion(f"ControlNet {controlnet_version.version_flag}", open = False, elem_id="controlnet"):
                if max_models > 1:
                    with gr.Tabs(elem_id=f"{elem_id_tabname}_tabs"):
                        for i in range(max_models):
                            with gr.Tab(f"ControlNet Unit {i}"):
                                controls += (self.uigroup(f"ControlNet-{i}", is_img2img, elem_id_tabname),)
                else:
                    with gr.Column():
                        controls += (self.uigroup(f"ControlNet", is_img2img, elem_id_tabname),)

        if shared.opts.data.get("control_net_sync_field_args", False):
            for _, field_name in self.infotext_fields:
                self.paste_field_names.append(field_name)

        return controls

    def register_modules(self, tabname, params):
        enabled, module, model, weight = params[4:8]
        guidance_start, guidance_end, guess_mode, pixel_perfect, control_mode = params[-5:]

        self.infotext_fields.extend([
            (enabled, f"{tabname} Enabled"),
            (module, f"{tabname} Preprocessor"),
            (model, f"{tabname} Model"),
            (weight, f"{tabname} Weight"),
            (guidance_start, f"{tabname} Guidance Start"),
            (guidance_end, f"{tabname} Guidance End"),
        ])

    def clear_control_model_cache(self):
        Script.model_cache.clear()
        gc.collect()
        devices.torch_gc()

    def load_control_model(self, p, unet, model, lowvram):
        if model in Script.model_cache:
            print(f"Loading model from cache: {model}")
            return Script.model_cache[model]

        # Remove model from cache to clear space before building another model
        if len(Script.model_cache) > 0 and len(Script.model_cache) >= shared.opts.data.get("control_net_model_cache_size", 2):
            Script.model_cache.popitem(last=False)
            gc.collect()
            devices.torch_gc()

        model_net = self.build_control_model(p, unet, model, lowvram)

        if shared.opts.data.get("control_net_model_cache_size", 2) > 0:
            Script.model_cache[model] = model_net

        return model_net

    def build_control_model(self, p, unet, model, lowvram):
        model_path = global_state.cn_models.get(model, None)
        if model_path is None:
            model = find_closest_lora_model_name(model)
            model_path = global_state.cn_models.get(model, None)

        if model_path is None:
            raise RuntimeError(f"model not found: {model}")

        # trim '"' at start/end
        if model_path.startswith("\"") and model_path.endswith("\""):
            model_path = model_path[1:-1]

        if not os.path.exists(model_path):
            raise ValueError(f"file not found: {model_path}")

        print(f"Loading model: {model}")
        state_dict = load_state_dict(model_path)
        network_module = PlugableControlModel
        network_config = shared.opts.data.get("control_net_model_config", global_state.default_conf)
        if not os.path.isabs(network_config):
            network_config = os.path.join(global_state.script_dir, network_config)

        if any([k.startswith("body.") or k == 'style_embedding' for k, v in state_dict.items()]):
            # adapter model
            network_module = PlugableAdapter
            network_config = shared.opts.data.get("control_net_model_adapter_config", global_state.default_conf_adapter)
            if not os.path.isabs(network_config):
                network_config = os.path.join(global_state.script_dir, network_config)

        model_path = os.path.abspath(model_path)
        model_stem = Path(model_path).stem
        model_dir_name = os.path.dirname(model_path)

        possible_config_filenames = [
            os.path.join(model_dir_name, model_stem + ".yaml"),
            os.path.join(global_state.script_dir, 'models', model_stem + ".yaml"),
            os.path.join(model_dir_name, model_stem.replace('_fp16', '') + ".yaml"),
            os.path.join(global_state.script_dir, 'models', model_stem.replace('_fp16', '') + ".yaml"),
            os.path.join(model_dir_name, model_stem.replace('_diff', '') + ".yaml"),
            os.path.join(global_state.script_dir, 'models', model_stem.replace('_diff', '') + ".yaml"),
            os.path.join(model_dir_name, model_stem.replace('-fp16', '') + ".yaml"),
            os.path.join(global_state.script_dir, 'models', model_stem.replace('-fp16', '') + ".yaml"),
            os.path.join(model_dir_name, model_stem.replace('-diff', '') + ".yaml"),
            os.path.join(global_state.script_dir, 'models', model_stem.replace('-diff', '') + ".yaml")
        ]

        override_config = possible_config_filenames[0]

        for possible_config_filename in possible_config_filenames:
            if os.path.exists(possible_config_filename):
                override_config = possible_config_filename
                break

        if 'v11' in model_stem.lower() or 'shuffle' in model_stem.lower():
            assert os.path.exists(override_config), f'Error: The model config {override_config} is missing. ControlNet 1.1 must have configs.'

        if os.path.exists(override_config):
            network_config = override_config

        print(f"Loading config: {network_config}")
        network = network_module(
            state_dict=state_dict,
            config_path=network_config,
            lowvram=lowvram,
            base_model=unet,
        )
        network.to(p.sd_model.device, dtype=p.sd_model.dtype)
        print(f"ControlNet model {model} loaded.")
        return network

    @staticmethod
    def get_remote_call(p, attribute, default=None, idx=0, strict=False, force=False):
        if not force and not shared.opts.data.get("control_net_allow_script_control", False):
            return default

        def get_element(obj, strict=False):
            if not isinstance(obj, list):
                return obj if not strict or idx == 0 else None
            elif idx < len(obj):
                return obj[idx]
            else:
                return None

        attribute_value = get_element(getattr(p, attribute, None), strict)
        default_value = get_element(default)
        return attribute_value if attribute_value is not None else default_value

    def parse_remote_call(self, p, unit: external_code.ControlNetUnit, idx):
        selector = self.get_remote_call

        unit.enabled = selector(p, "control_net_enabled", unit.enabled, idx, strict=True)
        unit.module = selector(p, "control_net_module", unit.module, idx)
        unit.model = selector(p, "control_net_model", unit.model, idx)
        unit.weight = selector(p, "control_net_weight", unit.weight, idx)
        unit.image = selector(p, "control_net_image", unit.image, idx)
        unit.resize_mode = selector(p, "control_net_resize_mode", unit.resize_mode, idx)
        unit.low_vram = selector(p, "control_net_lowvram", unit.low_vram, idx)
        unit.processor_res = selector(p, "control_net_pres", unit.processor_res, idx)
        unit.threshold_a = selector(p, "control_net_pthr_a", unit.threshold_a, idx)
        unit.threshold_b = selector(p, "control_net_pthr_b", unit.threshold_b, idx)
        unit.guidance_start = selector(p, "control_net_guidance_start", unit.guidance_start, idx)
        unit.guidance_end = selector(p, "control_net_guidance_end", unit.guidance_end, idx)
        unit.guidance_end = selector(p, "control_net_guidance_strength", unit.guidance_end, idx)
        unit.guess_mode = selector(p, "control_net_guess_mode", unit.guess_mode, idx)
        unit.pixel_perfect = selector(p, "control_net_pixel_perfect", unit.pixel_perfect, idx)

        return unit

    def detectmap_proc(self, detected_map, module, resize_mode, h, w):

        if 'inpaint' in module:
            detected_map = detected_map.astype(np.float32)
        else:
            detected_map = HWC3(detected_map)

        def safe_numpy(x):
            # A very safe method to make sure that Apple/Mac works
            y = x

            # below is very boring but do not change these. If you change these Apple or Mac may fail.
            y = y.copy()
            y = np.ascontiguousarray(y)
            y = y.copy()
            return y

        def get_pytorch_control(x):
            # A very safe method to make sure that Apple/Mac works
            y = x

            # below is very boring but do not change these. If you change these Apple or Mac may fail.
            y = torch.from_numpy(y)
            y = y.float() / 255.0
            y = rearrange(y, 'h w c -> c h w')
            y = y.clone()
            y = y.to(devices.get_device_for("controlnet"))
            y = y.clone()
            return y

        def high_quality_resize(x, size):
            # Written by lvmin
            # Super high-quality control map up-scaling, considering binary, seg, and one-pixel edges

            inpaint_mask = None
            if x.ndim == 3 and x.shape[2] == 4:
                inpaint_mask = x[:, :, 3]
                x = x[:, :, 0:3]

            new_size_is_smaller = (size[0] * size[1]) < (x.shape[0] * x.shape[1])
            new_size_is_bigger = (size[0] * size[1]) > (x.shape[0] * x.shape[1])
            unique_color_count = np.unique(x.reshape(-1, x.shape[2]), axis=0).shape[0]
            is_one_pixel_edge = False
            is_binary = False
            if unique_color_count == 2:
                is_binary = np.min(x) < 16 and np.max(x) > 240
                if is_binary:
                    xc = x
                    xc = cv2.erode(xc, np.ones(shape=(3, 3), dtype=np.uint8), iterations=1)
                    xc = cv2.dilate(xc, np.ones(shape=(3, 3), dtype=np.uint8), iterations=1)
                    one_pixel_edge_count = np.where(xc < x)[0].shape[0]
                    all_edge_count = np.where(x > 127)[0].shape[0]
                    is_one_pixel_edge = one_pixel_edge_count * 2 > all_edge_count

            if 2 < unique_color_count < 200:
                interpolation = cv2.INTER_NEAREST
            elif new_size_is_smaller:
                interpolation = cv2.INTER_AREA
            else:
                interpolation = cv2.INTER_CUBIC  # Must be CUBIC because we now use nms. NEVER CHANGE THIS

            y = cv2.resize(x, size, interpolation=interpolation)
            if inpaint_mask is not None:
                inpaint_mask = cv2.resize(inpaint_mask, size, interpolation=interpolation)

            if is_binary:
                y = np.mean(y.astype(np.float32), axis=2).clip(0, 255).astype(np.uint8)
                if is_one_pixel_edge:
                    y = nake_nms(y)
                    _, y = cv2.threshold(y, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                    y = lvmin_thin(y, prunings=new_size_is_bigger)
                else:
                    _, y = cv2.threshold(y, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                y = np.stack([y] * 3, axis=2)

            if inpaint_mask is not None:
                y[inpaint_mask > 127] = - 255

            return y

        if resize_mode == external_code.ResizeMode.RESIZE:
            detected_map = high_quality_resize(detected_map, (w, h))
            detected_map = safe_numpy(detected_map)
            return get_pytorch_control(detected_map), detected_map

        old_h, old_w, _ = detected_map.shape
        old_w = float(old_w)
        old_h = float(old_h)
        k0 = float(h) / old_h
        k1 = float(w) / old_w

        safeint = lambda x: int(np.round(x))

        if resize_mode == external_code.ResizeMode.OUTER_FIT:
            k = min(k0, k1)
            borders = np.concatenate([detected_map[0, :, 0:3], detected_map[-1, :, 0:3], detected_map[:, 0, 0:3], detected_map[:, -1, 0:3]], axis=0)
            high_quality_border_color = np.median(borders, axis=0).astype(detected_map.dtype)
            high_quality_background = np.tile(high_quality_border_color[None, None], [h, w, 1])
            detected_map = high_quality_resize(detected_map, (safeint(old_w * k), safeint(old_h * k)))
            new_h, new_w, _ = detected_map.shape
            pad_h = max(0, (h - new_h) // 2)
            pad_w = max(0, (w - new_w) // 2)
            high_quality_background[pad_h:pad_h + new_h, pad_w:pad_w + new_w] = detected_map
            detected_map = high_quality_background
            detected_map = safe_numpy(detected_map)
            return get_pytorch_control(detected_map), detected_map
        else:
            k = max(k0, k1)
            detected_map = high_quality_resize(detected_map, (safeint(old_w * k), safeint(old_h * k)))
            new_h, new_w, _ = detected_map.shape
            pad_h = max(0, (new_h - h) // 2)
            pad_w = max(0, (new_w - w) // 2)
            detected_map = detected_map[pad_h:pad_h+h, pad_w:pad_w+w]
            detected_map = safe_numpy(detected_map)
            return get_pytorch_control(detected_map), detected_map

    def is_ui(self, args):
        return args and all(isinstance(arg, UiControlNetUnit) for arg in args)

    def get_enabled_units(self, p):
        units = external_code.get_all_units_in_processing(p)
        enabled_units = []

        if len(units) == 0:
            # fill a null group
            remote_unit = self.parse_remote_call(p, self.get_default_ui_unit(), 0)
            if remote_unit.enabled:
                units.append(remote_unit)

        for idx, unit in enumerate(units):
            unit = self.parse_remote_call(p, unit, idx)
            if not unit.enabled:
                continue

            enabled_units.append(copy(unit))
            if len(units) != 1:
                prefix = f"ControlNet-{idx}"
            else:
                prefix = "ControlNet"

            p.extra_generation_params.update({
                f"{prefix} Enabled": True,
                f"{prefix} Module": unit.module,
                f"{prefix} Model": unit.model,
                f"{prefix} Weight": unit.weight,
                f"{prefix} Guidance Start": unit.guidance_start,
                f"{prefix} Guidance End": unit.guidance_end,
            })

        return enabled_units

    def process(self, p, *args):
        """
        This function is called before processing begins for AlwaysVisible scripts.
        You can modify the processing object (p) here, inject hooks, etc.
        args contains all values returned by components from ui()
        """
        unet = p.sd_model.model.diffusion_model
        if self.latest_network is not None:
            # always restore (~0.05s)
            self.latest_network.restore(unet)

        if not batch_hijack.instance.is_batch:
            self.enabled_units = self.get_enabled_units(p)

        if len(self.enabled_units) == 0:
           self.latest_network = None
           return

        detected_maps = []
        forward_params = []
        hook_lowvram = False

        # cache stuff
        if self.latest_model_hash != p.sd_model.sd_model_hash:
            self.clear_control_model_cache()

        # unload unused preproc
        module_list = [unit.module for unit in self.enabled_units]
        for key in self.unloadable:
            if key not in module_list:
                self.unloadable.get(key, lambda:None)()

        self.latest_model_hash = p.sd_model.sd_model_hash
        for idx, unit in enumerate(self.enabled_units):
            unit.module = self.get_module_basename(unit.module)
            p_input_image = self.get_remote_call(p, "control_net_input_image", None, idx)
            image = image_dict_from_any(unit.image)
            if image is not None:
                while len(image['mask'].shape) < 3:
                    image['mask'] = image['mask'][..., np.newaxis]

            resize_mode = external_code.resize_mode_from_value(unit.resize_mode)

            if unit.low_vram:
                hook_lowvram = True

            model_net = self.load_control_model(p, unet, unit.model, unit.low_vram)
            model_net.reset()

            if batch_hijack.instance.is_batch and getattr(p, "image_control", None) is not None:
                input_image = HWC3(np.asarray(p.image_control))
            elif p_input_image is not None:
                if isinstance(p_input_image, dict) and "mask" in p_input_image and "image" in p_input_image:
                    color = HWC3(np.asarray(p_input_image['image']))
                    alpha = np.asarray(p_input_image['mask'])[..., None]
                    input_image = np.concatenate([color, alpha], axis=2)
                else:
                    input_image = HWC3(np.asarray(p_input_image))
            elif image is not None:
                # Need to check the image for API compatibility
                if isinstance(image['image'], str):
                    from modules.api.api import decode_base64_to_image
                    input_image = HWC3(np.asarray(decode_base64_to_image(image['image'])))
                else:
                    input_image = HWC3(image['image'])

                have_mask = 'mask' in image and not ((image['mask'][:, :, 0] == 0).all() or (image['mask'][:, :, 0] == 255).all())

                if 'inpaint' in unit.module:
                    print("using inpaint as input")
                    color = HWC3(image['image'])
                    if have_mask:
                        alpha = image['mask'][:, :, 0:1]
                    else:
                        alpha = np.zeros_like(color)[:, :, 0:1]
                    input_image = np.concatenate([color, alpha], axis=2)
                else:
                    if have_mask:
                        print("using mask as input")
                        input_image = HWC3(image['mask'][:, :, 0])
                        unit.module = 'none'  # Always use black bg and white line
            else:
                # use img2img init_image as default
                input_image = getattr(p, "init_images", [None])[0]
                if input_image is None:
                    if batch_hijack.instance.is_batch:
                        shared.state.interrupted = True
                    raise ValueError('controlnet is enabled but no input image is given')

                input_image = HWC3(np.asarray(input_image))
                a1111_i2i_resize_mode = getattr(p, "resize_mode", None)
                if a1111_i2i_resize_mode is not None:
                    if a1111_i2i_resize_mode == 0:
                        resize_mode = external_code.ResizeMode.RESIZE
                    elif a1111_i2i_resize_mode == 1:
                        resize_mode = external_code.ResizeMode.INNER_FIT
                    elif a1111_i2i_resize_mode == 2:
                        resize_mode = external_code.ResizeMode.OUTER_FIT

            has_mask = False
            if input_image.ndim == 3:
                if input_image.shape[2] == 4:
                    if np.max(input_image[:, :, 3]) > 127:
                        has_mask = True

            a1111_mask = getattr(p, "image_mask", None)
            if 'inpaint' in unit.module and not has_mask and a1111_mask is not None:
                a1111_mask = a1111_mask.convert('L')
                if getattr(p, "inpainting_mask_invert", False):
                    a1111_mask = ImageOps.invert(a1111_mask)
                if getattr(p, "mask_blur", 0) > 0:
                    a1111_mask = a1111_mask.filter(ImageFilter.GaussianBlur(p.mask_blur))
                a1111_mask = np.asarray(a1111_mask)
                if a1111_mask.ndim == 2:
                    if a1111_mask.shape[0] == input_image.shape[0]:
                        if a1111_mask.shape[1] == input_image.shape[1]:
                            input_image = np.concatenate([input_image[:, :, 0:3], a1111_mask[:, :, None]], axis=2)
                            input_image = np.ascontiguousarray(input_image.copy()).copy()
                            a1111_i2i_resize_mode = getattr(p, "resize_mode", None)
                            if a1111_i2i_resize_mode is not None:
                                if a1111_i2i_resize_mode == 0:
                                    resize_mode = external_code.ResizeMode.RESIZE
                                elif a1111_i2i_resize_mode == 1:
                                    resize_mode = external_code.ResizeMode.INNER_FIT
                                elif a1111_i2i_resize_mode == 2:
                                    resize_mode = external_code.ResizeMode.OUTER_FIT

            if issubclass(type(p), StableDiffusionProcessingImg2Img) and p.inpaint_full_res == True and p.image_mask is not None:
                input_image = [input_image[:, :, i] for i in range(input_image.shape[2])]
                input_image = [Image.fromarray(x) for x in input_image]

                mask = p.image_mask.convert('L')
                if p.inpainting_mask_invert:
                    mask = ImageOps.invert(mask)
                if p.mask_blur > 0:
                    mask = mask.filter(ImageFilter.GaussianBlur(p.mask_blur))

                crop_region = masking.get_crop_region(np.array(mask), p.inpaint_full_res_padding)
                crop_region = masking.expand_crop_region(crop_region, p.width, p.height, mask.width, mask.height)

                if resize_mode == external_code.ResizeMode.INNER_FIT:
                    input_image = [images.resize_image(1, i, mask.width, mask.height) for i in input_image]
                elif resize_mode == external_code.ResizeMode.OUTER_FIT:
                    input_image = [images.resize_image(2, i, mask.width, mask.height) for i in input_image]
                else:
                    input_image = [images.resize_image(0, i, mask.width, mask.height) for i in input_image]

                input_image = [x.crop(crop_region) for x in input_image]
                input_image = [images.resize_image(2, x, p.width, p.height) for x in input_image]

                input_image = [np.asarray(x)[:, :, 0] for x in input_image]
                input_image = np.stack(input_image, axis=2)

            tmp_seed = int(p.all_seeds[0] if p.seed == -1 else max(int(p.seed),0))
            tmp_subseed = int(p.all_seeds[0] if p.subseed == -1 else max(int(p.subseed),0))
            np.random.seed((tmp_seed + tmp_subseed) & 0xFFFFFFFF)

            # safe numpy
            input_image = np.ascontiguousarray(input_image.copy()).copy()

            print(f"Loading preprocessor: {unit.module}")
            preprocessor = self.preprocessor[unit.module]
            h, w, bsz = p.height, p.width, p.batch_size
            if unit.processor_res > 64:
                preprocessor_resolution = unit.processor_res
                if unit.pixel_perfect:
                    raw_H, raw_W, _ = input_image.shape
                    target_H, target_W = p.height, p.width

                    k0 = float(target_H) / float(raw_H)
                    k1 = float(target_W) / float(raw_W)

                    if resize_mode == external_code.ResizeMode.OUTER_FIT:
                        estimation = min(k0, k1) * float(min(raw_H, raw_W))
                    else:
                        estimation = max(k0, k1) * float(min(raw_H, raw_W))

                    preprocessor_resolution = int(np.round(float(estimation) / 64.0)) * 64

                    print(f'Pixel Perfect Mode Enabled.')
                    print(f'resize_mode = {str(resize_mode)}')
                    print(f'raw_H = {raw_H}')
                    print(f'raw_W = {raw_W}')
                    print(f'target_H = {target_H}')
                    print(f'target_W = {target_W}')
                    print(f'estimation = {estimation}')

                print(f'preprocessor resolution = {preprocessor_resolution}')
                detected_map, is_image = preprocessor(input_image, res=preprocessor_resolution, thr_a=unit.threshold_a, thr_b=unit.threshold_b)
            else:
                detected_map, is_image = preprocessor(input_image)

            if unit.module == "none" and "style" in unit.model:
                detected_map_bytes = detected_map[:,:,0].tobytes()
                detected_map = np.ndarray((round(input_image.shape[0]/4),input_image.shape[1]),dtype="float32",buffer=detected_map_bytes)
                detected_map = torch.Tensor(detected_map).to(devices.get_device_for("controlnet"))
                is_image = False

            if isinstance(p, StableDiffusionProcessingTxt2Img) and p.enable_hr:
                if p.hr_resize_x == 0 and p.hr_resize_y == 0:
                    hr_y = int(p.height * p.hr_scale)
                    hr_x = int(p.width * p.hr_scale)
                else:
                    hr_y, hr_x = p.hr_resize_y, p.hr_resize_x

                if is_image:
                    hr_control, hr_detected_map = self.detectmap_proc(detected_map, unit.module, resize_mode, hr_y, hr_x)
                    detected_maps.append((hr_detected_map, unit.module))
                else:
                    hr_control = detected_map
            else:
                hr_control = None

            if is_image:
                control, detected_map = self.detectmap_proc(detected_map, unit.module, resize_mode, h, w)
                detected_maps.append((detected_map, unit.module))
            else:
                control = detected_map

                if unit.module == 'clip_vision':
                    fake_detected_map = np.ndarray((detected_map.shape[0]*4, detected_map.shape[1]),dtype="uint8", buffer=detected_map.detach().cpu().numpy().tobytes())
                    detected_maps.append((fake_detected_map, unit.module))

            is_vanilla_samplers = p.sampler_name in ["DDIM", "PLMS", "UniPC"]

            forward_param = ControlParams(
                control_model=model_net,
                hint_cond=control,
                guess_mode=unit.guess_mode,
                weight=unit.weight,
                guidance_stopped=False,
                start_guidance_percent=unit.guidance_start,
                stop_guidance_percent=unit.guidance_end,
                advanced_weighting=None,
                is_adapter=isinstance(model_net, PlugableAdapter),
                is_extra_cond=getattr(model_net, "target", "") == "scripts.adapter.StyleAdapter",
                global_average_pooling=model_net.config.model.params.get("global_average_pooling", False),
                hr_hint_cond=hr_control,
                batch_size=p.batch_size,
                instance_counter=0,
                is_vanilla_samplers=is_vanilla_samplers,
                cfg_scale=p.cfg_scale,
                soft_injection=unit.control_mode != external_code.ControlMode.BALANCED.value,
                cfg_injection=unit.control_mode == external_code.ControlMode.CONTROL.value
            )
            forward_params.append(forward_param)

            del model_net

        self.latest_network = UnetHook(lowvram=hook_lowvram)
        self.latest_network.hook(unet)
        self.latest_network.notify(forward_params, is_vanilla_samplers)
        self.detected_map = detected_maps

        if len(self.enabled_units) > 0 and shared.opts.data.get("control_net_skip_img2img_processing") and hasattr(p, "init_images"):
            swap_img2img_pipeline(p)

    def postprocess(self, p, processed, *args):
        if not batch_hijack.instance.is_batch:
            self.enabled_units.clear()

        if shared.opts.data.get("control_net_detectmap_autosaving", False) and self.latest_network is not None:
            for detect_map, module in self.detected_map:
                detectmap_dir = os.path.join(shared.opts.data.get("control_net_detectedmap_dir", ""), module)
                if not os.path.isabs(detectmap_dir):
                    detectmap_dir = os.path.join(p.outpath_samples, detectmap_dir)
                if module != "none":
                    os.makedirs(detectmap_dir, exist_ok=True)
                    img = Image.fromarray(np.ascontiguousarray(detect_map.clip(0, 255).astype(np.uint8)).copy())
                    save_image(img, detectmap_dir, module)

        if self.latest_network is None:
            return

        no_detectmap_opt = shared.opts.data.get("control_net_no_detectmap", False)
        if not batch_hijack.instance.is_batch and not no_detectmap_opt and self.detected_map:
            for detect_map, module in self.detected_map:
                if detect_map is None:
                    continue
                processed.images.extend([Image.fromarray(detect_map.clip(0, 255).astype(np.uint8))])

        self.input_image = None
        self.latest_network.restore(p.sd_model.model.diffusion_model)
        self.latest_network = None
        self.detected_map.clear()

        gc.collect()
        devices.torch_gc()

    def batch_tab_process(self, p, batches, *args, **kwargs):
        self.enabled_units = self.get_enabled_units(p)
        for unit_i, unit in enumerate(self.enabled_units):
            unit.batch_images = iter([batch[unit_i] for batch in batches])

    def batch_tab_process_each(self, p, *args, **kwargs):
        for unit_i, unit in enumerate(self.enabled_units):
            if getattr(unit, 'loopback', False) and batch_hijack.instance.batch_index > 0: continue

            unit.image = next(unit.batch_images)

    def batch_tab_postprocess_each(self, p, processed, *args, **kwargs):
        for unit_i, unit in enumerate(self.enabled_units):
            if getattr(unit, 'loopback', False):
                output_images = getattr(processed, 'images', [])[processed.index_of_first_image:]
                if output_images:
                    unit.image = np.array(output_images[0])
                else:
                    print(f'Warning: No loopback image found for controlnet unit {unit_i}. Using control map from last batch iteration instead')

    def batch_tab_postprocess(self, p, *args, **kwargs):
        self.enabled_units.clear()
        self.input_image = None
        if self.latest_network is None: return

        self.latest_network.restore(shared.sd_model.model.diffusion_model)
        self.latest_network = None
        self.detected_map.clear()


def on_ui_settings():
    section = ('control_net', "ControlNet")
    shared.opts.add_option("control_net_model_config", shared.OptionInfo(
        global_state.default_conf, "Config file for Control Net models", section=section))
    shared.opts.add_option("control_net_model_adapter_config", shared.OptionInfo(
        global_state.default_conf_adapter, "Config file for Adapter models", section=section))
    shared.opts.add_option("control_net_detectedmap_dir", shared.OptionInfo(
        global_state.default_detectedmap_dir, "Directory for detected maps auto saving", section=section))
    shared.opts.add_option("control_net_models_path", shared.OptionInfo(
        "", "Extra path to scan for ControlNet models (e.g. training output directory)", section=section))
    shared.opts.add_option("control_net_modules_path", shared.OptionInfo(
        "", "Path to directory containing annotator model directories (requires restart, overrides corresponding command line flag)", section=section))
    shared.opts.add_option("control_net_max_models_num", shared.OptionInfo(
        1, "Multi ControlNet: Max models amount (requires restart)", gr.Slider, {"minimum": 1, "maximum": 10, "step": 1}, section=section))
    shared.opts.add_option("control_net_model_cache_size", shared.OptionInfo(
        1, "Model cache size (requires restart)", gr.Slider, {"minimum": 1, "maximum": 5, "step": 1}, section=section))
    shared.opts.add_option("control_net_control_transfer", shared.OptionInfo(
        False, "Apply transfer control when loading models", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("control_net_no_detectmap", shared.OptionInfo(
        False, "Do not append detectmap to output", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("control_net_high_res_only_mid", shared.OptionInfo(
        False, "Use only-mid-control on high-res. fix (second pass)", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("control_net_detectmap_autosaving", shared.OptionInfo(
        False, "Allow detectmap auto saving", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("control_net_allow_script_control", shared.OptionInfo(
        False, "Allow other script to control this extension", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("control_net_skip_img2img_processing", shared.OptionInfo(
        False, "Skip img2img processing when using img2img initial image", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("control_net_monocular_depth_optim", shared.OptionInfo(
        False, "Enable optimized monocular depth estimation", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("control_net_only_mid_control", shared.OptionInfo(
        False, "Only use mid-control when inference", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("control_net_sync_field_args", shared.OptionInfo(
        False, "Passing ControlNet parameters with \"Send to img2img\"", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("controlnet_show_batch_images_in_ui", shared.OptionInfo(
        False, "Show batch images in gradio gallerie output", gr.Checkbox, {"interactive": True}, section=section))
    shared.opts.add_option("controlnet_increment_seed_during_batch", shared.OptionInfo(
        False, "Increment seed after each controlnet batch iteration", gr.Checkbox, {"interactive": True}, section=section))
    # shared.opts.add_option("control_net_advanced_weighting", shared.OptionInfo(
    #     False, "Enable advanced weight tuning", gr.Checkbox, {"interactive": False}, section=section))


def on_after_component(component, **_kwargs):
    global txt2img_submit_button
    if getattr(component, 'elem_id', None) == 'txt2img_generate':
        txt2img_submit_button = component
        return

    global img2img_submit_button
    if getattr(component, 'elem_id', None) == 'img2img_generate':
        img2img_submit_button = component
        return

    global img2img_batch_input_dir
    if getattr(component, 'elem_id', None) == 'img2img_batch_input_dir':
        img2img_batch_input_dir = component
        for callback in img2img_batch_input_dir_callbacks:
            callback()
        return

    global img2img_batch_output_dir
    if getattr(component, 'elem_id', None) == 'img2img_batch_output_dir':
        img2img_batch_output_dir = component
        for callback in img2img_batch_output_dir_callbacks:
            callback()
        return

    if getattr(component, 'elem_id', None) == 'img2img_batch_inpaint_mask_dir':
        global_batch_input_dir.render()
        return


batch_hijack.instance.do_hijack()
script_callbacks.on_ui_settings(on_ui_settings)
script_callbacks.on_after_component(on_after_component)
