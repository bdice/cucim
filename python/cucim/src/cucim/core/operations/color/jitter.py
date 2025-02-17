# Copyright (c) 2021, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numbers
from typing import Any

import cupy
import numpy as np

from .kernel.cuda_kernel_source import cuda_kernel_code

CUDA_KERNELS = cupy.RawModule(code=cuda_kernel_code)


def _check_input(
    value, name, center=1, bound=(0, float("inf")), clip_first_on_zero=True
):
    if isinstance(value, numbers.Number):
        if value < 0:
            raise ValueError(
                f"If {name} is a single number, \
                             it must be non negative."
            )
        value = [center - float(value), center + float(value)]
        if clip_first_on_zero:
            value[0] = max(value[0], 0.0)
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        if not bound[0] <= value[0] <= value[1] <= bound[1]:
            raise ValueError(f"{name} values should be between {bound}")
    else:
        raise TypeError(
            f"{name} should be a single number or a \
                        list/tuple with length 2."
        )
    # if value is 0 or (1., 1.) for brightness/contrast/saturation
    # or (0., 0.) for hue, do nothing
    if value[0] == value[1] == center:
        value = None
    return value


def _get_params(
    brightness: list[float] | None,
    contrast: list[float] | None,
    saturation: list[float] | None,
    hue: list[float] | None,
) -> tuple[np.ndarray, float | None, float | None, float | None, float | None,]:
    fn_idx = np.random.permutation(4)

    b = None
    if brightness is not None:
        b = float(np.random.uniform(brightness[0], brightness[1]))
    c = None
    if contrast is not None:
        c = float(np.random.uniform(contrast[0], contrast[1]))
    s = None
    if saturation is not None:
        s = float(np.random.uniform(saturation[0], saturation[1]))
    h = None
    if hue is not None:
        h = float(np.random.uniform(hue[0], hue[1]))

    return fn_idx, b, c, s, h


# brightness jitter
def _adjust_brightness(input_arr, brightness):
    if len(input_arr.shape) == 4:
        N, C, H, W = input_arr.shape
    elif len(input_arr.shape) == 3:
        C, H, W = input_arr.shape
        N = 1

    block = (128, 1, 1)
    length = N * C * H * W
    length = (length + 1) >> 2
    grid = (int((length - 1) / block[0] + 1), 1, 1)

    result = cupy.ndarray(shape=input_arr.shape, dtype=input_arr.dtype)
    kernel = CUDA_KERNELS.get_function("brightnessjitter_kernel")
    kernel(
        grid,
        block,
        args=(
            input_arr,
            result,
            np.int32(N * C * H * W),
            np.float32(brightness),
        ),
    )
    return result


# contrast jitter
def _adjust_contrast(input_arr, contrast):
    # contrast: 0.0 grey image, 1.0 original image
    # out RGB -> Grey
    # new image with mean L, convert to RGB
    # L -> RGB is just replicating L values across all channels
    # blend again as LHS

    if len(input_arr.shape) == 4:
        N, C, H, W = input_arr.shape
    elif len(input_arr.shape) == 3:
        C, H, W = input_arr.shape
        N = 1
    block = (128, 1, 1)
    pitch = W * H
    grid = (int((pitch - 1) / block[0] + 1), N, 1)

    output_L32 = cupy.empty((N, H, W), dtype=cupy.uint32)
    kernel_rgb2l = CUDA_KERNELS.get_function("rgb2l_kernel")
    kernel_rgb2l(grid, block, args=(input_arr, output_L32, np.int32(pitch)))

    L32_mean = output_L32.mean(axis=[1, 2], dtype=cupy.float32)

    if len(input_arr.shape) == 3:
        output_rgb = cupy.empty((C, H, W), dtype=cupy.uint8)
    else:
        output_rgb = cupy.empty((N, C, H, W), dtype=cupy.uint8)
    kernel_blendconstant = CUDA_KERNELS.get_function("blendconstant_kernel")
    kernel_blendconstant(
        grid,
        block,
        args=(
            input_arr,
            output_rgb,
            np.int32(pitch),
            L32_mean,
            np.float32(contrast),
        ),
    )

    return output_rgb


# saturation jitter
def _adjust_saturation(input_arr, saturation):
    # saturation (color enhance) 0.0 b/w image
    if len(input_arr.shape) == 4:
        N, C, H, W = input_arr.shape
    elif len(input_arr.shape) == 3:
        C, H, W = input_arr.shape
        N = 1

    pitch = W * H
    block = (128, 1, 1)
    grid = (int((pitch - 1) / block[0] + 1), N, 1)

    output_rgb = cupy.empty(input_arr.shape, dtype=cupy.uint8)
    kernel_satjitter = CUDA_KERNELS.get_function("saturationjitter_kernel")
    kernel_satjitter(
        grid,
        block,
        args=(input_arr, output_rgb, np.int32(pitch), np.float32(saturation)),
    )

    return output_rgb


# hue jitter
def _adjust_hue(input_arr, hue):
    if not (-0.5 <= hue <= 0.5):
        raise ValueError(f"hue factor({hue}) is not in [-0.5, 0.5].")

    if len(input_arr.shape) == 4:
        N, C, H, W = input_arr.shape
    elif len(input_arr.shape) == 3:
        C, H, W = input_arr.shape
        N = 1

    pitch = W * H
    block = (128, 1, 1)
    grid = (int((pitch - 1) / block[0] + 1), N, 1)
    output_rgb = cupy.empty(input_arr.shape, dtype=cupy.uint8)
    kernel_huejitter = CUDA_KERNELS.get_function("huejitter_kernel")
    kernel_huejitter(
        grid,
        block,
        args=(input_arr, output_rgb, np.int32(pitch), np.float32(hue)),
    )

    return output_rgb


def color_jitter(img: Any, brightness=0, contrast=0, saturation=0, hue=0):
    """Applies color jitter by random sequential application of
    4 operations (brightness, contrast, saturation, hue).

    Parameters
    ----------
    img : channel first, cupy.ndarray or numpy.ndarray
        Input data of shape (C, H, W). Can also batch process input of shape
        (N, C, H, W). Can be a numpy.ndarray or cupy.ndarray.
    brightness : float or 2-tuple of float, optional
        Non-negative factor to jitter the brightness by. When `brightness` is a
        scalar, scaling will be by a random value in range
        ``[max(0, 1 - brightness), (1 + brightness)]``. `brightness` can
        also be a 2-tuple specifying the range for the random scaling factor.
        A value of 0 or (1, 1) will result in no change.
    contrast : float or 2-tuple of float, optional
        Non-negative factor to jitter the contrast by. When `contrast` is a
        scalar, scaling will be by a random value between
        ``[max(0, 1 - contrast), (1 + contrast)]``. `contrast` can
        also be a 2-tuple specifying the range for the random scaling factor.
        A value of 0 or (1, 1) will result in no change.
    saturation : float or 2-tuple of float, optional
        Non-negative factor to jitter the saturation by. When `saturation` is a
        scalar, scaling will be by a random value between
        ``[max(0, 1 - saturation), (1 + saturation)]``. `saturation` can
        also be a 2-tuple specifying the range for the random scaling factor.
        A value of 0 or (1, 1) will result in no change.
    hue : float or 2-tuple of float, optional
        Factor between [-0.5, 0.5] to jitter hue by. When `hue` is a
        scalar, scaling will be by a random value between in the range
        ``[-hue, hue]``. `hue` can also be a 2-tuple specifying the range.
        A value of 0 or (0, 0) will result in no change.

    Returns
    -------
    out : cupy.ndarray or numpy.ndarray
        Output data. Same dimensions and type as input.

    Raises
    ------
    ValueError
        If 'brightness','contrast','saturation' or 'hue' is outside
        of allowed range
    TypeError
        If input 'img' is not cupy.ndarray or numpy.ndarray

    Examples
    --------
    >>> import cucim.core.operations.color as ccl
    >>> # input is channel first 3d array
    >>> output_array = ccl.color_jitter(input_arr,.25,.75,.25,.04)
    """
    # TODO: should be a class stateful implementation to caches values
    #       once instead of checking every time

    # execution
    f_brightness = _check_input(brightness, "brightness")
    f_contrast = _check_input(contrast, "contrast")
    f_saturation = _check_input(saturation, "saturation")
    f_hue = _check_input(
        hue, "hue", center=0, bound=(-0.5, 0.5), clip_first_on_zero=False
    )

    to_numpy = False
    if isinstance(img, np.ndarray):
        to_numpy = True
        cupy_img = cupy.asarray(img, dtype=cupy.uint8, order="C")
    elif not isinstance(img, cupy.ndarray):
        raise TypeError("img must be a cupy.ndarray or numpy.ndarray")
    else:
        cupy_img = cupy.ascontiguousarray(img)

    if cupy_img.dtype != cupy.uint8:
        if cupy.can_cast(cupy_img.dtype, cupy.uint8, "unsafe") is False:
            raise ValueError(
                "Cannot cast type {cupy_img.dtype.name} to 'uint8'"
            )
        else:
            cupy_img = cupy_img.astype(cupy.uint8)

    if img.ndim not in (3, 4):
        raise ValueError(
            f"Unsupported img.ndim={img.ndim}. Expected `img` with "
            "dimensions (C, H, W) or (N, C, H, W)."
        )

    (
        fn_idx,
        brightness_factor,
        contrast_factor,
        saturation_factor,
        hue_factor,
    ) = _get_params(f_brightness, f_contrast, f_saturation, f_hue)

    for fn_id in fn_idx:
        if fn_id == 0 and brightness_factor is not None:
            cupy_img = _adjust_brightness(cupy_img, brightness_factor)
        elif fn_id == 1 and contrast_factor is not None:
            cupy_img = _adjust_contrast(cupy_img, contrast_factor)
        elif fn_id == 2 and saturation_factor is not None:
            cupy_img = _adjust_saturation(cupy_img, saturation_factor)
        elif fn_id == 3 and hue_factor is not None:
            cupy_img = _adjust_hue(cupy_img, hue_factor)

    if img.dtype != np.uint8:
        cupy_img = cupy_img.astype(cupy.float32)

    result = cupy_img
    if to_numpy:
        result = cupy.asnumpy(cupy_img)

    return result


def rand_color_jitter(
    img: Any,
    brightness=0,
    contrast=0,
    saturation=0,
    hue=0,
    prob: float = 0.1,
    whole_batch: bool = False,
):
    """Randomly applies color jitter by random sequential application of
    4 operations (brightness, contrast, saturation, hue).

    Parameters
    ----------
    img : channel first, cupy.ndarray or numpy.ndarray
        Input data of shape (C, H, W). Can also batch process input of shape
        (N, C, H, W). Can be a numpy.ndarray or cupy.ndarray.
    brightness : float or 2-tuple of float, optional
        Non-negative factor to jitter the brightness by. When `brightness` is a
        scalar, scaling will be by a random value in range
        ``[max(0, 1 - brightness), (1 + brightness)]``. `brightness` can
        also be a 2-tuple specifying the range for the random scaling factor.
        A value of 0 or (1, 1) will result in no change.
    contrast : float or 2-tuple of float, optional
        Non-negative factor to jitter the contrast by. When `contrast` is a
        scalar, scaling will be by a random value between
        ``[max(0, 1 - contrast), (1 + contrast)]``. `contrast` can
        also be a 2-tuple specifying the range for the random scaling factor.
        A value of 0 or (1, 1) will result in no change.
    saturation : float or 2-tuple of float, optional
        Non-negative factor to jitter the saturation by. When `saturation` is a
        scalar, scaling will be by a random value between
        ``[max(0, 1 - saturation), (1 + saturation)]``. `saturation` can
        also be a 2-tuple specifying the range for the random scaling factor.
        A value of 0 or (1, 1) will result in no change.
    hue : float or 2-tuple of float, optional
        Factor between [-0.5, 0.5] to jitter hue by. When `hue` is a
        scalar, scaling will be by a random value between in the range
        ``[-hue, hue]``. `hue` can also be a 2-tuple specifying the range.
        A value of 0 or (0, 0) will result in no change.
    prob: probability of applying color jitter.
        (Default 0.1, with 10% probability it returns a color jittered array)
    whole_batch: Flag to apply transform on whole batch.
        If False, each image in the batch is randomly transformed
        It True, entire batch is transformed
    Returns
    -------
    out : cupy.ndarray or numpy.ndarray
        Output data. Same dimensions and type as input.

    Raises
    ------
    ValueError
        If 'brightness','contrast','saturation' or 'hue' is outside
        of allowed range
    TypeError
        If input 'img' is not cupy.ndarray or numpy.ndarray

    Examples
    --------
    >>> import cucim.core.operations.color as ccl
    >>> # input is channel first 3d array
    >>> output_array = ccl.rand_color_jitter(input_arr,.25,.75,.25,.04)
    """
    R = np.random.RandomState()

    shape = img.shape
    image_wise_probs = []

    if whole_batch is False and len(shape) == 4:
        image_wise_probs = R.rand(shape[0])

        for i in range(shape[0]):
            if image_wise_probs[i] < prob:
                img[i] = color_jitter(
                    img[i], brightness, contrast, saturation, hue
                )
        return img
    else:
        return color_jitter(img, brightness, contrast, saturation, hue)
