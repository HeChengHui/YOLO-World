# Copyright (c) Tencent Inc. All rights reserved.
import os
import cv2
import argparse
import os.path as osp

import torch
import supervision as sv
from mmengine.config import Config, DictAction
from mmengine.runner import Runner
from mmengine.runner.amp import autocast
from mmengine.dataset import Compose
from mmengine.utils import ProgressBar
from mmyolo.registry import RUNNERS

BOUNDING_BOX_ANNOTATOR = sv.BoundingBoxAnnotator()
LABEL_ANNOTATOR = sv.LabelAnnotator()


def parse_args():
    parser = argparse.ArgumentParser(description='YOLO-World Demo')
    parser.add_argument('config', help='test config file path')
    parser.add_argument('checkpoint', help='checkpoint file')
    parser.add_argument('image', help='image path, include image file or dir.')
    parser.add_argument(
        'text',
        help=
        'text prompts, including categories separated by a comma or a txt file with each line as a promopt.'
    )
    parser.add_argument('--topk', default=100, type=int, help='keep topk predictions.')
    parser.add_argument('--threshold', default=0.0, type=float, help='confidence score threshold for predictions.')
    parser.add_argument('--device',
                        default='cuda:0',
                        help='device used for inference.')
    parser.add_argument('--show',
                        action='store_true',
                        help='show the detection results.')
    parser.add_argument('--amp',
                        action='store_true',
                        help='use mixed precision for inference.')
    parser.add_argument('--output-dir',
                        default='demo_outputs',
                        help='the directory to save outputs')
    parser.add_argument(
        '--cfg-options',
        nargs='+',
        action=DictAction,
        help='override some settings in the used config, the key-value pair '
        'in xxx=yyy format will be merged into config file. If the value to '
        'be overwritten is a list, it should be like key="[a,b]" or key=a,b '
        'It also allows nested list/tuple values, e.g. key="[(a,b),(c,d)]" '
        'Note that the quotation marks are necessary and that no white space '
        'is allowed.')
    args = parser.parse_args()
    return args


def inference_detector(runner,
                       image_path,
                       texts,
                       max_dets,
                       score_thr,
                       output_dir,
                       use_amp=False,
                       show=False):

    data_info = dict(img_id=0, img_path=image_path, texts=texts)
    data_info = runner.pipeline(data_info)
    data_batch = dict(inputs=data_info['inputs'].unsqueeze(0),
                      data_samples=[data_info['data_samples']])

    with autocast(enabled=use_amp), torch.no_grad():
        output = runner.model.test_step(data_batch)[0]
        pred_instances = output.pred_instances
        pred_instances = pred_instances[
            pred_instances.scores.float() > score_thr]
    if len(pred_instances.scores) > max_dets:
        indices = pred_instances.scores.float().topk(max_dets)[1]
        pred_instances = pred_instances[indices]

    pred_instances = pred_instances.cpu().numpy()
    detections = sv.Detections(xyxy=pred_instances['bboxes'],
                               class_id=pred_instances['labels'],
                               confidence=pred_instances['scores'])

    labels = [
        f"{texts[class_id][0]} {confidence:0.2f}" for class_id, confidence in
        zip(detections.class_id, detections.confidence)
    ]

    # label images
    image = cv2.imread(image_path)
    image = BOUNDING_BOX_ANNOTATOR.annotate(image, detections)
    image = LABEL_ANNOTATOR.annotate(image, detections, labels=labels)
    cv2.imwrite(osp.join(output_dir, osp.basename(image_path)), image)

    if show:
        cv2.imshow(image)
        k = cv2.waitKey(0)
        if k == 27:
            # wait for ESC key to exit
            cv2.destroyAllWindows()


if __name__ == '__main__':
    args = parse_args()

    # load config
    cfg = Config.fromfile(args.config)
    if args.cfg_options is not None:
        cfg.merge_from_dict(args.cfg_options)

    cfg.work_dir = osp.join('./work_dirs',
                            osp.splitext(osp.basename(args.config))[0])

    cfg.load_from = args.checkpoint

    if 'runner_type' not in cfg:
        runner = Runner.from_cfg(cfg)
    else:
        runner = RUNNERS.build(cfg)

    # load text
    if args.text.endswith('.txt'):
        with open(args.text) as f:
            lines = f.readlines()
        texts = [[t.rstrip('\r\n')] for t in lines] + [[' ']]
    else:
        texts = [[t.strip()] for t in args.text.split(',')] + [[' ']]

    output_dir = args.output_dir
    if not osp.exists(output_dir):
        os.mkdir(output_dir)

    runner.call_hook('before_run')
    runner.load_or_resume()
    pipeline = cfg.test_dataloader.dataset.pipeline
    runner.pipeline = Compose(pipeline)
    runner.model.eval()

    if not osp.isfile(args.image):
        images = [
            osp.join(args.image, img) for img in os.listdir(args.image)
            if img.endswith('.png') or img.endswith('.jpg')
        ]
    else:
        images = [args.image]

    progress_bar = ProgressBar(len(images))
    for image_path in images:

        inference_detector(runner,
                           image_path,
                           texts,
                           args.topk,
                           args.threshold,
                           output_dir=output_dir,
                           use_amp=args.amp,
                           show=args.show)
        progress_bar.update()
