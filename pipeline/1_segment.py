#!/usr/bin/env python3
"""
Step 1: Image Segmentation using SAM3

Takes input frames and generates segmentation masks for specified objects.
"""

import argparse
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import sam3
from sam3 import build_sam3_image_model
from sam3.model.sam3_image_processor import Sam3Processor

from path_resolver import PathResolver


def overlay_masks_on_image(image, masks, boxes, scores, colors=None):
    """Overlay segmentation masks on the original image."""
    if isinstance(image, Image.Image):
        frame = np.array(image)
    else:
        frame = image.copy()
    
    if len(frame.shape) == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
    elif frame.shape[2] == 4:
        frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2RGB)
    
    num_objects = masks.shape[0]
    
    if colors is None:
        np.random.seed(42)
        colors = np.random.randint(0, 255, size=(num_objects, 3), dtype=np.uint8)
    
    masks_np = masks.squeeze(1).cpu().float().numpy().astype(np.uint8)
    boxes_np = boxes.cpu().float().numpy()
    scores_np = scores.cpu().float().numpy()
    
    for idx, (mask, box, score, color) in enumerate(zip(masks_np, boxes_np, scores_np, colors)):
        curr_masked_frame = np.where(mask[..., None], color, frame)
        frame = cv2.addWeighted(frame, 0.75, curr_masked_frame, 0.25, 0)
        
        contours, _ = cv2.findContours(mask.copy(), cv2.RETR_TREE, cv2.CHAIN_APPROX_NONE)
        cv2.drawContours(frame, contours, -1, (255, 255, 255), 7)
        cv2.drawContours(frame, contours, -1, (0, 0, 0), 5)
        cv2.drawContours(frame, contours, -1, color.tolist(), 3)
        
        x0, y0, x1, y1 = box.astype(int)
        cv2.rectangle(frame, (x0, y0), (x1, y1), color.tolist(), 2)
        
        label = f"ID:{idx} ({score:.2f})"
        font = cv2.FONT_HERSHEY_SIMPLEX
        (text_width, text_height), _ = cv2.getTextSize(label, font, 0.5, 1)
        cv2.rectangle(frame, (x0, y0 - text_height - 10), (x0 + text_width + 5, y0), color.tolist(), -1)
        cv2.putText(frame, label, (x0 + 2, y0 - 5), font, 0.5, (255, 255, 255), 1)
    
    return frame


def segment_image(image_path, text_prompt, model, processor, confidence_threshold, output_dir):
    """Segment objects in a single image."""
    print(f"  Processing: {Path(image_path).name}")
    
    image = Image.open(image_path)
    width, height = image.size
    
    processor.confidence_threshold = confidence_threshold
    inference_state = processor.set_image(image)
    processor.reset_all_prompts(inference_state)
    inference_state = processor.set_text_prompt(state=inference_state, prompt=text_prompt)
    
    masks = inference_state.get('masks')
    boxes = inference_state.get('boxes')
    scores = inference_state.get('scores')
    
    num_objects = 0 if masks is None else masks.shape[0]
    print(f"    Detected {num_objects} object(s)")
    
    if num_objects > 0 and output_dir is not None:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Save overlay
        overlay = overlay_masks_on_image(image, masks, boxes, scores)
        Image.fromarray(overlay).save(output_path / "segmentation_overlay.png")
        
        # Save individual masks
        for idx in range(num_objects):
            mask = masks[idx, 0].cpu().float().numpy().astype(np.uint8) * 255
            mask_filename = f"mask_{idx:03d}_score_{scores[idx].item():.3f}.png"
            Image.fromarray(mask).save(output_path / mask_filename)
        
        # Save numpy arrays
        np.save(output_path / "all_masks.npy", masks.cpu().float().numpy())
        np.save(output_path / "boxes.npy", boxes.cpu().float().numpy())
        np.save(output_path / "scores.npy", scores.cpu().float().numpy())
    
    return {
        'masks': masks,
        'boxes': boxes,
        'scores': scores,
        'num_objects': num_objects,
    }


def run_segmentation(resolver: PathResolver):
    """Run segmentation on all frames."""
    
    print("=" * 60)
    print("STEP 1: SEGMENTATION")
    print("=" * 60)
    
    # Get config values
    prompt = resolver.get('segmentation', 'prompt', default='object')
    confidence = resolver.get('segmentation', 'confidence_threshold', default=0.5)
    
    frames_dir = resolver.frames_dir
    output_dir = resolver.masks_dir
    
    print(f"Frames directory: {frames_dir}")
    print(f"Output directory: {output_dir}")
    print(f"Prompt: '{prompt}'")
    print(f"Confidence threshold: {confidence}")
    
    # Check frames directory
    if not frames_dir.exists():
        raise FileNotFoundError(f"Frames directory not found: {frames_dir}")
    
    # Setup device
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    
    if device == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        torch.autocast("cuda", dtype=torch.bfloat16).__enter__()
    
    # Build model
    print("\nLoading SAM3 model...")
    sam3_root = os.path.join(os.path.dirname(sam3.__file__), "..")
    bpe_path = os.path.join(sam3_root, "sam3", "assets", "bpe_simple_vocab_16e6.txt.gz")
    model = build_sam3_image_model(bpe_path=bpe_path)
    processor = Sam3Processor(model, confidence_threshold=confidence)
    
    # Find images
    supported_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp'}
    image_files = [f for f in frames_dir.iterdir() 
                   if f.is_file() and f.suffix.lower() in supported_extensions]
    image_files = sorted(image_files)
    
    if not image_files:
        raise FileNotFoundError(f"No images found in {frames_dir}")
    
    print(f"\nFound {len(image_files)} images")
    print("-" * 60)
    
    # Process each image
    total_objects = 0
    for idx, image_path in enumerate(image_files, 1):
        print(f"\n[{idx}/{len(image_files)}]", end="")
        
        image_output_dir = output_dir / image_path.stem
        
        try:
            results = segment_image(
                image_path=str(image_path),
                text_prompt=prompt,
                model=model,
                processor=processor,
                confidence_threshold=confidence,
                output_dir=image_output_dir
            )
            total_objects += results['num_objects']
        except Exception as e:
            print(f"    ERROR: {e}")
    
    print("\n" + "=" * 60)
    print(f"STEP 1 COMPLETE: {total_objects} objects detected across {len(image_files)} images")
    print(f"Output saved to: {output_dir}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Step 1: Segmentation")
    parser.add_argument("--config", type=str, required=True, help="Path to config YAML")
    args = parser.parse_args()
    
    resolver = PathResolver(args.config)
    resolver.setup_run()
    
    run_segmentation(resolver)


if __name__ == "__main__":
    main()