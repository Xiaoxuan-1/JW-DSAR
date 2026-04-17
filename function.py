# -*- coding: utf-8 -*-
import os
import argparse
import matplotlib.pyplot as plt
from PIL import Image
from astropy.io import fits
from scipy.signal import *
import numpy as np
from scipy.stats import skew, kurtosis
from scipy import ndimage
import pywt
from skimage import measure
import shutil
import cv2
import concurrent.futures
import imageio.v2 as imageio
import time
from skimage.color import rgb2gray
from multiprocessing import Pool
from multiprocessing import Process, cpu_count, Lock, Pool
import json
import logging
from tqdm import tqdm
from pathlib import Path

def fluxValues(magnetogram):
    # compute sum of positive and negative values,
    # then evaluate a signed and unsigned sum.
    posSum = np.sum(magnetogram[magnetogram > 0])
    negSum = np.sum(magnetogram[magnetogram < 0])
    signSum = posSum + negSum
    unsignSum = posSum - negSum

    return posSum, negSum, signSum, unsignSum


def gradient(image):
    # use sobel operators to find the gradient
    sobelx = [[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]]
    sobely = [[1, 2, 1], [0, 0, 0], [-1, -2, -1]]

    gx = convolve2d(image, sobelx, mode='same')
    gy = convolve2d(image, sobely, mode='same')

    M = (gx ** 2 + gy ** 2) ** (1. / 2)

    return M


def Gradfeat(image):
    # evaluate statistics of the gradient image
    res = gradient(image).flatten()
    men = np.mean(res)
    strd = np.std(res)
    med = np.median(res)
    minim = np.amin(res)
    maxim = np.amax(res)
    skw = skew(res)
    kurt = kurtosis(res)
    return men, strd, med, minim, maxim, skw, kurt


def wavel(image):
    # create wavelet transform array for display
    # can be added to the return statement for
    # visualization
    wt = pywt.wavedecn(image, 'haar', level=5)
    arr, coeff_slices = pywt.coeffs_to_array(wt)

    # compute wavelet energy
    LL, L5, L4, L3, L2, L1 = pywt.wavedec2(image, 'haar', level=5)
    L1e = np.sum(np.absolute(L1))
    L2e = np.sum(np.absolute(L2))
    L3e = np.sum(np.absolute(L3))
    L4e = np.sum(np.absolute(L4))
    L5e = np.sum(np.absolute(L5))

    return L1e, L2e, L3e, L4e, L5e


def extractNL(image):
    avg10 = (1. / 100) * np.ones([10, 10])
    avgim = convolve2d(image, avg10, mode='same')
    out = measure.find_contours(avgim, level=0)
    return out


def NLmaskgen(contours, image):
    mask = np.zeros((image.shape))
    for n, contour in enumerate(contours):
        # print(n,contour)
        for i in range(len(contour)):
            y = int(round(contour[i, 1]))
            x = int(round(contour[i, 0]))
            mask[x, y] = 1.
    return mask


def findTGWNL(image):
    m = 0.2 * np.amax(np.absolute(image))
    width = image.shape[0]
    height = image.shape[1]
    out = np.zeros([height, width])
    out[abs(image) >= m] = 1

    return out


def curvature(contour):
    angles = np.zeros([contour.shape[0]])
    yvals = np.around(contour[:, 1])
    xvals = np.around(contour[:, 0])
    for i in range(contour.shape[0]):
        if i < contour.shape[0] - 1:
            n = i + 1
        else:
            n = 0
        y = int(yvals[i])
        x = int(xvals[i])
        yn = int(yvals[n])
        xn = int(xvals[n])
        num = yn - y
        den = xn - x
        if den != 0:
            angles[i] = np.arctan(num / den)
        elif num < 0:
            angles[i] = 3 * np.pi / 2
        else:
            angles[i] = np.pi / 2
    return angles


def bendergy(angles):
    fact = 1. / len(angles)
    count = 0.
    for i in range(len(angles)):
        if i < len(angles) - 1:
            n = i + 1
        else:
            n = 0
        T = angles[i]
        Tn = angles[n]
        count += (T - Tn) ** 2

    BE = count * fact
    return BE


def NLfeat(image):
    grad = gradient(image)
    contours = extractNL(image)
    ma = NLmaskgen(contours, image)
    gwnl = np.zeros([grad.shape[0], grad.shape[1]])
    gwnl = grad * ma
    thresh = findTGWNL(gwnl)
    NLlen = np.sum(thresh)

    struct = [[1, 1, 1], [1, 1, 1], [1, 1, 1]]
    lines, numlines = ndimage.label(thresh, struct)

    GWNLlen = np.sum(ma)
    Flag = True
    if not contours:
        return 0., 0., 0., 0., 0., 0., 0., 0., 0., 0., 0., 0., 0.
    else:
        for n, contour in enumerate(contours):
            curve = curvature(contour)
            if Flag:
                angstore = np.zeros([len(curve)])
                angstore = curve
                BEstore = np.zeros([len(contours)])
                Flag = False
            else:
                angstore = np.concatenate((curve, angstore))
            BEstore[n] = bendergy(curve)

    return float(NLlen), float(numlines), float(GWNLlen), float(np.mean(angstore)), np.std(angstore), np.median(
        angstore), np.amin(angstore), np.amax(angstore), np.mean(BEstore), np.std(BEstore), np.median(BEstore), np.amin(
        BEstore), np.amax(BEstore)


def mdi_norm(image_file):
    import numpy as np
    # image_file = removenan(image_file)
    arr1 = (image_file > 200)
    image_file[arr1] = 200
    arr0 = (image_file < -200)
    image_file[arr0] = -200
    image_file[np.isnan(image_file)] = -200
    min, max = np.min(image_file), np.max(image_file)
    mdi_mag = (((image_file - min) / (max - min)) * 255).astype(np.uint8)
    return mdi_mag


def fittopng(fitfile):
    with fits.open(fitfile) as Img:
        Img.verify('silentfix')
        Img = Img[1].data
        Img = mdi_norm(Img)
    return Img


def generate_txt(txtname, sync):
    name_list = ['Gradient mean', 'Gradient std', 'Gradient median', 'Gradient min', 'Gradient max',
                 'Gradient skewness', 'Gradient kurtosis', 'NL length', 'NL no. fragments',
                 'NL gradient-weighted length',
                 'NL curvature mean', 'NL curvature std', 'NL curvature median', 'NL curvature min', 'NL curvature max',
                 'NL bending energy mean', 'NL bending energy std', 'NL bending energy median', 'NL bending energy min',
                 'NL bending energy max',
                 'Wavelet Energy L1', 'Wavelet Energy L2', 'Wavelet Energy L3', 'Wavelet Energy L4',
                 'Wavelet Energy L5',
                 'Total positive flux', 'Total negative flux', 'Total signed flux', 'Total unsigned flux']
    for i in range(len(name_list) - 1):
        with open(txtname, 'a') as f:
            f.write('<' + name_list[i] + ':' + sync[i] + '>, ')
    with open(txtname, 'a') as f:
        f.write('<' + name_list[28] + ':' + sync[28] + '>')


def process_txt(img600_path, txt_path, ARitem):
    logging.info(f'Starting processing for {ARitem}')
    
    for imgitem in os.listdir(os.path.join(img600_path, ARitem)):
        imgname = os.path.join(img600_path, ARitem, imgitem)
        txtname = os.path.join(txt_path, ARitem, imgitem.split('.png')[0] + '.txt')

        if not os.path.exists(os.path.join(txt_path, ARitem)):
            os.makedirs(os.path.join(txt_path, ARitem))
        
        if os.path.exists(txtname):
            logging.info(f'Skipping existing file: {txtname}')
            continue
        
        try:
            Img = imageio.imread(imgname).astype(float)

            # Check if the image has the expected number of channels
            if Img.ndim == 3 and Img.shape[2] == 4:  # RGBA
                weights = np.array([0.299, 0.587, 0.114, 0])  # Ignore alpha channel
                gray_image_weighted_avg = np.sum(Img[:, :, :3] * weights[:3], axis=2)  # Use only RGB channels
            elif Img.ndim == 3 and Img.shape[2] == 3:  # RGB
                weights = np.array([0.299, 0.587, 0.114])
                gray_image_weighted_avg = np.sum(Img * weights, axis=2)
            elif Img.ndim == 2:  # Grayscale
                gray_image_weighted_avg = Img
            else:
                raise ValueError(f'Unexpected image shape: {Img.shape}')

            image = gray_image_weighted_avg - 128  # Offset for zero flux

            G = Gradfeat(image)
            NL = NLfeat(image)
            wav = wavel(image)
            F = fluxValues(image)
            res = np.concatenate((G, NL, wav, F))
            res = [" ".join(str('%.3f' % x)) for x in res]
            generate_txt(txtname, res)
            logging.info(f'Generate file: {txtname}')
        except Exception as e:
            logging.error(f'Error processing {imgname}: {e}', exc_info=True)
    
    logging.info(f'txtdone: {ARitem}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Generate JW-Flare feature text files from PNG series.")
    parser.add_argument(
        "--image-dir",
        type=str,
        default=str(Path(__file__).resolve().parent / "data" / "img_600"),
        help="Input directory containing AR subfolders with PNG files.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(Path(__file__).resolve().parent / "data" / "txt_600"),
        help="Output directory for generated text feature files.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=str(Path(__file__).resolve().parent / "data" / "process.log"),
        help="Log file path.",
    )
    args = parser.parse_args()

    img600_path = os.path.abspath(args.image_dir)
    txt_path = os.path.abspath(args.output_dir)
    log_file = os.path.abspath(args.log_file)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format='%(asctime)s:%(levelname)s:%(message)s',
    )
    if not os.path.isdir(img600_path):
        raise SystemExit(f"Image directory not found: {img600_path}")
    os.makedirs(txt_path, exist_ok=True)
    ARitemlist = sorted(os.listdir(img600_path))

    logging.info('Starting the processing script')

    # 使用 tqdm 创建进度条
    with concurrent.futures.ProcessPoolExecutor(max_workers=cpu_count()) as executor:
        # 将进度条应用于 executor.map
        list(tqdm(executor.map(process_txt, [img600_path]*len(ARitemlist), [txt_path]*len(ARitemlist), ARitemlist), total=len(ARitemlist)))

