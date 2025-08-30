print("🟡 [CROSSWALK_UTILS] This is d:/Downloads/finale6/Khatam final/khatam/qt_app_pyside/utils/crosswalk_utils.py LOADED")
import cv2
import numpy as np
from typing import Tuple, Optional

def detect_crosswalk_and_violation_line(frame: np.ndarray, traffic_light_position: Optional[Tuple[int, int]] = None):
    """
    Detects crosswalk (zebra crossing) or fallback stop line in a traffic scene using classical CV.
    Args:
        frame: BGR image frame from video feed
        traffic_light_position: Optional (x, y) of traffic light in frame
    Returns:
        crosswalk_bbox: (x, y, w, h) or None if fallback used
        violation_line_y: int (y position for violation check)
        debug_info: dict (for visualization/debugging)
    """
    debug_info = {}
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    # --- Preprocessing for zebra crossing ---
    # Enhance contrast for night/low-light
    if np.mean(gray) < 80:
        gray = cv2.equalizeHist(gray)
        debug_info['hist_eq'] = True
    else:
        debug_info['hist_eq'] = False
    # Adaptive threshold to isolate white stripes
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                   cv2.THRESH_BINARY, 19, 7)
    # Morphology to connect stripes
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3))
    morph = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=2)
    # Find contours
    contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    zebra_rects = []
    for cnt in contours:
        x, y, rw, rh = cv2.boundingRect(cnt)
        area = rw * rh
        aspect = rw / rh if rh > 0 else 0
        # Heuristic: long, thin, bright, horizontal stripes
        if area > 500 and 2 < aspect < 15 and rh < h * 0.15:
            zebra_rects.append((x, y, rw, rh))
    debug_info['zebra_rects'] = zebra_rects
    # Group rectangles that are aligned horizontally (zebra crossing)
    crosswalk_bbox = None
    violation_line_y = None
    if len(zebra_rects) >= 3:
        # Sort by y, then group by proximity
        zebra_rects = sorted(zebra_rects, key=lambda r: r[1])
        groups = []
        group = [zebra_rects[0]]
        for rect in zebra_rects[1:]:
            if abs(rect[1] - group[-1][1]) < 40:  # 40px vertical tolerance
                group.append(rect)
            else:
                if len(group) >= 3:
                    groups.append(group)
                group = [rect]
        if len(group) >= 3:
            groups.append(group)
        # Pick group closest to traffic light (if provided), else lowest in frame
        def group_center_y(g):
            return np.mean([r[1] + r[3] // 2 for r in g])
        if groups:
            if traffic_light_position:
                tx, ty = traffic_light_position
                best_group = min(groups, key=lambda g: abs(group_center_y(g) - ty))
            else:
                best_group = max(groups, key=group_center_y)
            # Union bbox
            xs = [r[0] for r in best_group] + [r[0] + r[2] for r in best_group]
            ys = [r[1] for r in best_group] + [r[1] + r[3] for r in best_group]
            x1, x2 = min(xs), max(xs)
            y1, y2 = min(ys), max(ys)
            crosswalk_bbox = (x1, y1, x2 - x1, y2 - y1)
            # Violation line: just before crosswalk starts (bottom of bbox - margin)
            violation_line_y = y2 - 5
            debug_info['crosswalk_group'] = best_group
    # --- Fallback: Stop line detection ---
    if crosswalk_bbox is None:
        edges = cv2.Canny(gray, 80, 200)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80, minLineLength=60, maxLineGap=20)
        stop_lines = []
        if lines is not None:
            for l in lines:
                x1, y1, x2, y2 = l[0]
                angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                if abs(angle) < 20 or abs(angle) > 160:  # horizontal
                    if y1 > h // 2 or y2 > h // 2:  # lower half
                        stop_lines.append((x1, y1, x2, y2))
        debug_info['stop_lines'] = stop_lines
        if stop_lines:
            # Pick the lowest (closest to bottom or traffic light)
            if traffic_light_position:
                tx, ty = traffic_light_position
                best_line = min(stop_lines, key=lambda l: abs(((l[1]+l[3])//2) - ty))
            else:
                best_line = max(stop_lines, key=lambda l: max(l[1], l[3]))
            x1, y1, x2, y2 = best_line
            crosswalk_bbox = None
            violation_line_y = min(y1, y2) - 5
            debug_info['stop_line'] = best_line
    return crosswalk_bbox, violation_line_y, debug_info

# Example usage:
# bbox, vline, dbg = detect_crosswalk_and_violation_line(frame, (tl_x, tl_y))
print("🟡 [CROSSWALK_UTILS] This is d:/Downloads/finale6/Khatam final/khatam/qt_app_pyside/utils/crosswalk_utils.py LOADED")
import cv2
import numpy as np
from typing import Dict, List, Tuple, Optional, Any
import math
# --- DeepLabV3+ Crosswalk Segmentation Integration ---
import sys
import os
sys.path.append(r'D:\Downloads\finale6\Khatam final\khatam\qt_app_pyside\DeepLabV3Plus-Pytorch')
import torch
import torch.nn as nn
from PIL import Image
from torchvision import transforms as T


def detect_crosswalk(frame: np.ndarray, roi_height_percentage: float = 0.4) -> Optional[List[int]]:
    """
    [DEPRECATED] Use detect_and_draw_crosswalk for advanced visualization and analytics.
    This function is kept for backward compatibility but will print a warning.
    """
    print("[WARN] detect_crosswalk is deprecated. Use detect_and_draw_crosswalk instead.")
    try:
        height, width = frame.shape[:2]
        roi_height = int(height * roi_height_percentage)
        roi_y = height - roi_height
        
        # Extract ROI
        roi = frame[roi_y:height, 0:width]
        
        # Convert to grayscale
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        
        # Apply adaptive thresholding
        binary = cv2.adaptiveThreshold(
            gray, 
            255, 
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
            cv2.THRESH_BINARY, 
            19, 
            2
        )
        
        # Apply morphological operations to clean up the binary image
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
        
        # Find contours
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        # Filter contours by shape and aspect ratio
        potential_stripes = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            aspect_ratio = w / h if h > 0 else 0
            area = cv2.contourArea(contour)
            
            # Stripe criteria: Rectangular, wide, not too tall
            if area > 100 and aspect_ratio >= 3 and aspect_ratio <= 20:
                potential_stripes.append((x, y + roi_y, w, h))
        
        # Group nearby stripes into crosswalk
        if len(potential_stripes) >= 3:
            # Sort by y-coordinate (top to bottom)
            potential_stripes.sort(key=lambda s: s[1])
            
            # Find groups of stripes with similar y-positions
            stripe_groups = []
            current_group = [potential_stripes[0]]
            
            for i in range(1, len(potential_stripes)):
                # If this stripe is close to the previous one in y-direction
                if abs(potential_stripes[i][1] - current_group[-1][1]) < 50:
                    current_group.append(potential_stripes[i])
                else:
                    # Start a new group
                    if len(current_group) >= 3:
                        stripe_groups.append(current_group)
                    current_group = [potential_stripes[i]]
            
            # Add the last group if it has enough stripes
            if len(current_group) >= 3:
                stripe_groups.append(current_group)
            
            # Find the largest group
            if stripe_groups:
                largest_group = max(stripe_groups, key=len)
                
                # Compute bounding box for the crosswalk
                min_x = min(stripe[0] for stripe in largest_group)
                min_y = min(stripe[1] for stripe in largest_group)
                max_x = max(stripe[0] + stripe[2] for stripe in largest_group)
                max_y = max(stripe[1] + stripe[3] for stripe in largest_group)
                
                return [min_x, min_y, max_x, max_y]
                
        return None
    except Exception as e:
        print(f"Error detecting crosswalk: {e}")
        return None

def detect_stop_line(frame: np.ndarray) -> Optional[int]:
    """
    Detect stop line in a frame using edge detection and Hough Line Transform.
    
    Args:
        frame: Input video frame
        
    Returns:
        Y-coordinate of the stop line or None if not detected
    """
    try:
        height, width = frame.shape[:2]
        
        # Define ROI - bottom 30% of the frame
        roi_height = int(height * 0.3)
        roi_y = height - roi_height
        roi = frame[roi_y:height, 0:width].copy()
        
        # Convert to grayscale
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        
        # Apply Gaussian blur to reduce noise
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        
        # Apply Canny edge detection
        edges = cv2.Canny(blurred, 50, 150)
        
        # Apply Hough Line Transform
        lines = cv2.HoughLinesP(
            edges, 
            rho=1, 
            theta=np.pi/180, 
            threshold=80, 
            minLineLength=width//3,  # Lines should be at least 1/3 of image width
            maxLineGap=50
        )
        
        if lines is None or len(lines) == 0:
            return None
            
        # Filter horizontal lines (slope close to 0)
        horizontal_lines = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            if x2 - x1 == 0:  # Avoid division by zero
                continue
                
            slope = abs((y2 - y1) / (x2 - x1))
            
            # Horizontal line has slope close to 0
            if slope < 0.2:
                horizontal_lines.append((x1, y1, x2, y2, slope))
        
        if not horizontal_lines:
            return None
            
        # Sort by y-coordinate (bottom to top)
        horizontal_lines.sort(key=lambda line: max(line[1], line[3]), reverse=True)
        
        # Get the uppermost horizontal line
        if horizontal_lines:
            x1, y1, x2, y2, _ = horizontal_lines[0]
            stop_line_y = roi_y + max(y1, y2)
            return stop_line_y
            
        return None
    except Exception as e:
        print(f"Error detecting stop line: {e}")
        return None

def draw_violation_line(frame: np.ndarray, y_coord: int, color: Tuple[int, int, int] = (0, 0, 255), 
                       label: str = "VIOLATION LINE", thickness: int = 2) -> np.ndarray:
    """
    Draw a violation line on the frame with customizable label.
    
    Args:
        frame: Input video frame
        y_coord: Y-coordinate for the line
        color: Line color (BGR)
        label: Custom label text to display
        thickness: Line thickness
        
    Returns:
        Frame with the violation line drawn
    """
    height, width = frame.shape[:2]
    cv2.line(frame, (0, y_coord), (width, y_coord), color, thickness)
    
    # Add label with transparent background for better visibility
    text_size, _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
    text_x = width // 2 - text_size[0] // 2
    text_y = y_coord - 10
    
    # Draw semi-transparent background
    overlay = frame.copy()
    cv2.rectangle(
        overlay, 
        (text_x - 5, text_y - text_size[1] - 5), 
        (text_x + text_size[0] + 5, text_y + 5), 
        (0, 0, 0), 
        -1
    )
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    
    # Add label
    cv2.putText(
        frame,
        label,
        (text_x, text_y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        color,
        2
    )
    
    return frame

def check_vehicle_violation(vehicle_bbox: List[int], violation_line_y: int) -> bool:
    """
    Check if a vehicle has crossed the violation line.
    
    Args:
        vehicle_bbox: Vehicle bounding box [x1, y1, x2, y2]
        violation_line_y: Y-coordinate of the violation line
        
    Returns:
        True if violation detected, False otherwise
    """
    # Get the bottom-center point of the vehicle
    x1, y1, x2, y2 = vehicle_bbox
    vehicle_bottom = y2
    vehicle_center_y = (y1 + y2) / 2
    
    # Calculate how much of the vehicle is below the violation line
    height = y2 - y1
    if height <= 0:  # Avoid division by zero
        return False
        
    # A vehicle is considered in violation if either:
    # 1. Its bottom edge is below the violation line
    # 2. Its center is below the violation line (for large vehicles)
    is_violation = (vehicle_bottom > violation_line_y) or (vehicle_center_y > violation_line_y)
    
    if is_violation:
        print(f"🚨 Vehicle crossing violation line! Vehicle bottom: {vehicle_bottom}, Line: {violation_line_y}")
        
    return is_violation

def get_deeplab_model(weights_path, device='cpu', model_name='deeplabv3plus_mobilenet', num_classes=21, output_stride=8):
    """
    Loads DeepLabV3+ model and weights for crosswalk segmentation.
    """
    print(f"[DEBUG] get_deeplab_model called with weights_path={weights_path}, device={device}, model_name={model_name}")
    import network  # DeepLabV3Plus-Pytorch/network/__init__.py
    model = network.modeling.__dict__[model_name](num_classes=num_classes, output_stride=output_stride)
    if weights_path is not None and os.path.isfile(weights_path):
        print(f"[DEBUG] Loading weights from: {weights_path}")
        checkpoint = torch.load(weights_path, map_location=torch.device(device))
        model.load_state_dict(checkpoint["model_state"])
    else:
        print(f"[DEBUG] Weights file not found: {weights_path}")
    model = nn.DataParallel(model)
    model.to(device)
    model.eval()
    print(f"[DEBUG] Model loaded and moved to {device}")
    return model

def run_inference(model, frame, device='cpu'):
    """
    Preprocesses frame and runs DeepLabV3+ model to get mask.
    """
    # frame: np.ndarray (H, W, 3) in BGR
    img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(img_rgb)
    transform = T.Compose([
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    input_tensor = transform(pil_img).unsqueeze(0).to(device)
    with torch.no_grad():
        output = model(input_tensor)
        if isinstance(output, dict):
            output = output["out"] if "out" in output else list(output.values())[0]
        mask = output.argmax(1).squeeze().cpu().numpy().astype(np.uint8)
    return mask

def detect_and_draw_crosswalk(frame: np.ndarray, roi_height_percentage: float = 0.4, use_deeplab: bool = True) -> Tuple[np.ndarray, Optional[List[int]], Optional[List]]:
    """
    Advanced crosswalk detection with DeepLabV3+ segmentation (if enabled),
    otherwise falls back to Hough Transform + line clustering.
    
    Args:
        frame: Input video frame
        roi_height_percentage: Percentage of the frame height to use as ROI
        use_deeplab: If True, use DeepLabV3+ segmentation for crosswalk detection
        
    Returns:
        Tuple containing:
        - Annotated frame with crosswalk visualization
        - Crosswalk bounding box [x, y, w, h] or None if not detected
        - List of detected crosswalk contours or lines or None
    """
    try:
        height, width = frame.shape[:2]
        annotated_frame = frame.copy()
        print(f"[DEBUG] detect_and_draw_crosswalk called, use_deeplab={use_deeplab}")
        # --- DeepLabV3+ Segmentation Path ---
        if use_deeplab:
            # Load model only once (cache in function attribute)
            if not hasattr(detect_and_draw_crosswalk, '_deeplab_model'):
                weights_path = os.path.join(os.path.dirname(__file__), '../DeepLabV3Plus-Pytorch/best_crosswalk.pth')
                print(f"[DEBUG] Loading DeepLabV3+ model from: {weights_path}")
                detect_and_draw_crosswalk._deeplab_model = get_deeplab_model(weights_path, device='cpu')
            model = detect_and_draw_crosswalk._deeplab_model
            # Run inference
            mask = run_inference(model, frame)
            print(f"[DEBUG] DeepLabV3+ mask shape: {mask.shape}, unique values: {np.unique(mask)}")
            # Assume crosswalk class index is 12 (change if needed)
            crosswalk_class = 12
            crosswalk_mask = (mask == crosswalk_class).astype(np.uint8) * 255
            print(f"[DEBUG] crosswalk_mask unique values: {np.unique(crosswalk_mask)}")
            # Find contours in mask
            contours, _ = cv2.findContours(crosswalk_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            print(f"[DEBUG] DeepLabV3+ found {len(contours)} contours")
            if not contours:
                print("[DEBUG] No contours found in DeepLabV3+ mask, falling back to classic method.")
                # Fallback to classic method if nothing found
                return detect_and_draw_crosswalk(frame, roi_height_percentage, use_deeplab=False)
            # Draw all crosswalk contours
            x_min, y_min, x_max, y_max = width, height, 0, 0
            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                x_min = min(x_min, x)
                y_min = min(y_min, y)
                x_max = max(x_max, x + w)
                y_max = max(y_max, y + h)
                cv2.drawContours(annotated_frame, [cnt], -1, (0, 255, 255), 3)
            # Clamp bbox to frame and ensure non-negative values
            x_min = max(0, min(x_min, width - 1))
            y_min = max(0, min(y_min, height - 1))
            x_max = max(0, min(x_max, width - 1))
            y_max = max(0, min(y_max, height - 1))
            w = max(0, x_max - x_min)
            h = max(0, y_max - y_min)
            crosswalk_bbox = [x_min, y_min, w, h]
            # Ignore invalid bboxes
            if w <= 0 or h <= 0:
                print("[DEBUG] Ignoring invalid crosswalk_bbox (zero or negative size)")
                return annotated_frame, None, contours
            # TODO: Mask out detected vehicles before running crosswalk detection to reduce false positives.
            cv2.rectangle(
                annotated_frame,
                (crosswalk_bbox[0], crosswalk_bbox[1]),
                (crosswalk_bbox[0] + crosswalk_bbox[2], crosswalk_bbox[1] + crosswalk_bbox[3]),
                (0, 255, 255), 2
            )
            cv2.putText(
                annotated_frame,
                "CROSSWALK",
                (crosswalk_bbox[0], crosswalk_bbox[1] - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2
            )
            print(f"[DEBUG] DeepLabV3+ crosswalk_bbox: {crosswalk_bbox}")
            return annotated_frame, crosswalk_bbox, contours
        # --- Classic Hough Transform Fallback ---
        print("[DEBUG] Using classic Hough Transform fallback method.")
        height, width = frame.shape[:2]
        roi_height = int(height * roi_height_percentage)
        roi_y = height - roi_height
        roi = frame[roi_y:height, 0:width]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=60, minLineLength=40, maxLineGap=30)
        print(f"[DEBUG] HoughLinesP found {0 if lines is None else len(lines)} lines")
        if lines is None:
            return frame, None, None
        angle_threshold = 12  # degrees
        parallel_lines = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if -angle_threshold <= angle <= angle_threshold or 80 <= abs(angle) <= 100:
                parallel_lines.append((x1, y1, x2, y2, angle))
        print(f"[DEBUG] {len(parallel_lines)} parallel lines after angle filtering")
        if len(parallel_lines) < 3:
            return frame, None, None
        parallel_lines = sorted(parallel_lines, key=lambda l: min(l[1], l[3]))
        clusters = []
        cluster = [parallel_lines[0]]
        min_spacing = 10
        max_spacing = 60
        for i in range(1, len(parallel_lines)):
            prev_y = min(cluster[-1][1], cluster[-1][3])
            curr_y = min(parallel_lines[i][1], parallel_lines[i][3])
            spacing = abs(curr_y - prev_y)
            if min_spacing < spacing < max_spacing:
                cluster.append(parallel_lines[i])
            else:
                if len(cluster) >= 3:
                    clusters.append(cluster)
                cluster = [parallel_lines[i]]
        if len(cluster) >= 3:
            clusters.append(cluster)
        print(f"[DEBUG] {len(clusters)} clusters found")
        if not clusters:
            return frame, None, None
        best_cluster = max(clusters, key=len)
        x_min = width
        y_min = roi_height
        x_max = 0
        y_max = 0
        for x1, y1, x2, y2, angle in best_cluster:
            cv2.line(annotated_frame, (x1, y1 + roi_y), (x2, y2 + roi_y), (0, 255, 255), 3)
            x_min = min(x_min, x1, x2)
            y_min = min(y_min, y1, y2)
            x_max = max(x_max, x1, x2)
            y_max = max(y_max, y1, y2)
        crosswalk_bbox = [x_min, y_min + roi_y, x_max - x_min, y_max - y_min]
        cv2.rectangle(
            annotated_frame,
            (crosswalk_bbox[0], crosswalk_bbox[1]),
            (crosswalk_bbox[0] + crosswalk_bbox[2], crosswalk_bbox[1] + crosswalk_bbox[3]),
            (0, 255, 255), 2
        )
        cv2.putText(
            annotated_frame,
            "CROSSWALK",
            (crosswalk_bbox[0], crosswalk_bbox[1] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 255),
            2
        )
        print(f"[DEBUG] Classic method crosswalk_bbox: {crosswalk_bbox}")
        return annotated_frame, crosswalk_bbox, best_cluster
    except Exception as e:
        print(f"Error in detect_and_draw_crosswalk: {str(e)}")
        import traceback
        traceback.print_exc()
        return frame, None, None


#working
print("🟡 [CROSSWALK_UTILS] This is d:/Downloads/finale6/Khatam final/khatam/qt_app_pyside/utils/crosswalk_utils.py LOADED")
import cv2
import numpy as np
from typing import Tuple, Optional

def detect_crosswalk_and_violation_line(frame: np.ndarray, traffic_light_position: Optional[Tuple[int, int]] = None, perspective_M: Optional[np.ndarray] = None):
    """
    Detects crosswalk (zebra crossing) or fallback stop line in a traffic scene using classical CV.
    Args:
        frame: BGR image frame from video feed
        traffic_light_position: Optional (x, y) of traffic light in frame
        perspective_M: Optional 3x3 homography matrix for bird's eye view normalization
    Returns:
        result_frame: frame with overlays (for visualization)
        crosswalk_bbox: (x, y, w, h) or None if fallback used
        violation_line_y: int (y position for violation check)
        debug_info: dict (for visualization/debugging)
    """
    debug_info = {}
    orig_frame = frame.copy()
    h, w = frame.shape[:2]

    # 1. Perspective Normalization (Bird's Eye View)
    if perspective_M is not None:
        frame = cv2.warpPerspective(frame, perspective_M, (w, h))
        debug_info['perspective_warped'] = True
    else:
        debug_info['perspective_warped'] = False

    # 1. White Color Filtering (relaxed)
    mask_white = cv2.inRange(frame, (160, 160, 160), (255, 255, 255))
    debug_info['mask_white_ratio'] = np.sum(mask_white > 0) / (h * w)

    # 2. Grayscale for adaptive threshold
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # Enhance contrast for night/low-light
    if np.mean(gray) < 80:
        gray = cv2.equalizeHist(gray)
        debug_info['hist_eq'] = True
    else:
        debug_info['hist_eq'] = False
    # 5. Adaptive threshold (tuned)
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, 15, 5)
    # Combine with color mask
    combined = cv2.bitwise_and(thresh, mask_white)
    # 2. Morphology (tuned)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3))
    morph = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, kernel, iterations=1)
    # Find contours
    contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    zebra_rects = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        aspect_ratio = w / max(h, 1)
        area = w * h
        angle = 0  # For simplicity, assume horizontal stripes
        # Heuristic: wide, short, and not too small
        if aspect_ratio > 3 and 1000 < area < 0.5 * frame.shape[0] * frame.shape[1] and h < 60:
            zebra_rects.append((x, y, w, h, angle))
            cv2.rectangle(orig_frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
    # --- Overlay drawing for debugging: draw all zebra candidates ---
    for r in zebra_rects:
        x, y, rw, rh, _ = r
        cv2.rectangle(orig_frame, (x, y), (x+rw, y+rh), (0, 255, 0), 2)
    # Draw all zebra candidate rectangles for debugging (no saving)
    for r in zebra_rects:
        x, y, rw, rh, _ = r
        cv2.rectangle(orig_frame, (x, y), (x+rw, y+rh), (0, 255, 0), 2)
    # --- Probabilistic Scoring for Groups ---
    def group_score(group):
        if len(group) < 3:
            return 0
        heights = [r[3] for r in group]
        x_centers = [r[0] + r[2]//2 for r in group]
        angles = [r[4] for r in group]
        # Stripe count (normalized)
        count_score = min(len(group) / 6, 1.0)
        # Height consistency
        height_score = 1.0 - min(np.std(heights) / (np.mean(heights) + 1e-6), 1.0)
        # X-center alignment
        x_score = 1.0 - min(np.std(x_centers) / (w * 0.2), 1.0)
        # Angle consistency (prefer near 0 or 90)
        mean_angle = np.mean([abs(a) for a in angles])
        angle_score = 1.0 - min(np.std(angles) / 10.0, 1.0)
        # Whiteness (mean mask_white in group area)
        whiteness = 0
        for r in group:
            x, y, rw, rh, _ = r
            whiteness += np.mean(mask_white[y:y+rh, x:x+rw]) / 255
        whiteness_score = whiteness / len(group)
        # Final score (weighted sum)
        score = 0.25*count_score + 0.2*height_score + 0.2*x_score + 0.15*angle_score + 0.2*whiteness_score
        return score
    # 4. Dynamic grouping tolerance
    y_tolerance = int(h * 0.05)
    crosswalk_bbox = None
    violation_line_y = None
    best_score = 0
    best_group = None
    if len(zebra_rects) >= 3:
        zebra_rects = sorted(zebra_rects, key=lambda r: r[1])
        groups = []
        group = [zebra_rects[0]]
        for rect in zebra_rects[1:]:
            if abs(rect[1] - group[-1][1]) < y_tolerance:
                group.append(rect)
            else:
                if len(group) >= 3:
                    groups.append(group)
                group = [rect]
        if len(group) >= 3:
            groups.append(group)
        # Score all groups
        scored_groups = [(group_score(g), g) for g in groups if group_score(g) > 0.1]
        print(f"[CROSSWALK DEBUG] scored_groups: {[s for s, _ in scored_groups]}")
        if scored_groups:
            scored_groups.sort(reverse=True, key=lambda x: x[0])
            best_score, best_group = scored_groups[0]
            print("Best group score:", best_score)
            # Visualization for debugging
            debug_vis = orig_frame.copy()
            for r in zebra_rects:
                x, y, rw, rh, _ = r
                cv2.rectangle(debug_vis, (x, y), (x+rw, y+rh), (255, 0, 255), 2)
            for r in best_group:
                x, y, rw, rh, _ = r
                cv2.rectangle(debug_vis, (x, y), (x+rw, y+rh), (0, 255, 255), 3)
            cv2.imwrite(f"debug_crosswalk_group.png", debug_vis)
            # Optionally, filter by vanishing point as before
            # ...existing vanishing point code...
            xs = [r[0] for r in best_group] + [r[0] + r[2] for r in best_group]
            ys = [r[1] for r in best_group] + [r[1] + r[3] for r in best_group]
            x1, x2 = min(xs), max(xs)
            y1, y2 = min(ys), max(ys)
            crosswalk_bbox = (x1, y1, x2 - x1, y2 - y1)
            violation_line_y = y2 - 5
            debug_info['crosswalk_group'] = best_group
            debug_info['crosswalk_score'] = best_score
            debug_info['crosswalk_angles'] = [r[4] for r in best_group]
    # --- Fallback: Stop line detection ---
    if crosswalk_bbox is None:
        edges = cv2.Canny(gray, 80, 200)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80, minLineLength=60, maxLineGap=20)
        stop_lines = []
        if lines is not None:
            for l in lines:
                x1, y1, x2, y2 = l[0]
                angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                if abs(angle) < 20 or abs(angle) > 160:  # horizontal
                    if y1 > h // 2 or y2 > h // 2:  # lower half
                        stop_lines.append((x1, y1, x2, y2))
        debug_info['stop_lines'] = stop_lines
        print(f"[CROSSWALK DEBUG] stop_lines: {len(stop_lines)} found")
        if stop_lines:
            if traffic_light_position:
                tx, ty = traffic_light_position
                best_line = min(stop_lines, key=lambda l: abs(((l[1]+l[3])//2) - ty))
            else:
                best_line = max(stop_lines, key=lambda l: max(l[1], l[3]))
            x1, y1, x2, y2 = best_line
            crosswalk_bbox = None
            violation_line_y = min(y1, y2) - 5
            debug_info['stop_line'] = best_line
            print(f"[CROSSWALK DEBUG] using stop_line: {best_line}")
    # Draw fallback violation line overlay for debugging (no saving)
    if crosswalk_bbox is None and violation_line_y is not None:
        print(f"[DEBUG] Drawing violation line at y={violation_line_y} (frame height={orig_frame.shape[0]})")
        if 0 <= violation_line_y < orig_frame.shape[0]:
            orig_frame = draw_violation_line(orig_frame, violation_line_y, color=(0, 255, 255), thickness=8, style='solid', label='Fallback Stop Line')
        else:
            print(f"[WARNING] Invalid violation line position: {violation_line_y}")
    # --- Manual overlay for visualization pipeline test ---
    # Removed fake overlays that could overwrite the real violation line
    print(f"[CROSSWALK DEBUG] crosswalk_bbox: {crosswalk_bbox}, violation_line_y: {violation_line_y}")
    return orig_frame, crosswalk_bbox, violation_line_y, debug_info

def draw_violation_line(frame: np.ndarray, y: int, color=(0, 255, 255), thickness=8, style='solid', label='Violation Line'):
    """
    Draws a thick, optionally dashed, labeled violation line at the given y-coordinate.
    Args:
        frame: BGR image
        y: y-coordinate for the line
        color: BGR color tuple
        thickness: line thickness
        style: 'solid' or 'dashed'
        label: Optional label to draw above the line
    Returns:
        frame with line overlay
    """
    import cv2
    h, w = frame.shape[:2]
    x1, x2 = 0, w
    overlay = frame.copy()
    if style == 'dashed':
        dash_len = 30
        gap = 20
        for x in range(x1, x2, dash_len + gap):
            x_end = min(x + dash_len, x2)
            cv2.line(overlay, (x, y), (x_end, y), color, thickness, lineType=cv2.LINE_AA)
    else:
        cv2.line(overlay, (x1, y), (x2, y), color, thickness, lineType=cv2.LINE_AA)
    # Blend for semi-transparency
    cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)
    # Draw label
    if label:
        font = cv2.FONT_HERSHEY_SIMPLEX
        text_size, _ = cv2.getTextSize(label, font, 0.8, 2)
        text_x = max(10, (w - text_size[0]) // 2)
        text_y = max(0, y - 12)
        cv2.rectangle(frame, (text_x - 5, text_y - text_size[1] - 5), (text_x + text_size[0] + 5, text_y + 5), (0,0,0), -1)
        cv2.putText(frame, label, (text_x, text_y), font, 0.8, color, 2, cv2.LINE_AA)
    return frame

def get_violation_line_y(frame, traffic_light_bbox=None, crosswalk_bbox=None):
    """
    Returns the y-coordinate of the violation line using the following priority:
    1. Crosswalk bbox (most accurate)
    2. Stop line detection via image processing (CV)
    3. Traffic light bbox heuristic
    4. Fallback (default)
    """
    height, width = frame.shape[:2]
    # 1. Crosswalk bbox
    if crosswalk_bbox is not None and len(crosswalk_bbox) == 4:
        return int(crosswalk_bbox[1]) - 15
    # 2. Stop line detection (CV)
    roi_height = int(height * 0.4)
    roi_y = height - roi_height
    roi = frame[roi_y:height, 0:width]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 15, -2
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 1))
    processed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    stop_line_candidates = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        aspect_ratio = w / max(h, 1)
        normalized_width = w / width
        if (aspect_ratio > 5 and normalized_width > 0.3 and h < 15 and y > roi_height * 0.5):
            abs_y = y + roi_y
            stop_line_candidates.append((abs_y, w))
    if stop_line_candidates:
        stop_line_candidates.sort(key=lambda x: x[1], reverse=True)
        return stop_line_candidates[0][0]
    # 3. Traffic light bbox heuristic
    if traffic_light_bbox is not None and len(traffic_light_bbox) == 4:
        traffic_light_bottom = traffic_light_bbox[3]
        traffic_light_height = traffic_light_bbox[3] - traffic_light_bbox[1]
        estimated_distance = min(5 * traffic_light_height, height * 0.3)
        return min(int(traffic_light_bottom + estimated_distance), height - 20)
    # 4. Fallback
    return int(height * 0.75)

# Example usage:
# bbox, vline, dbg = detect_crosswalk_and_violation_line(frame, (tl_x, tl_y), perspective_M)
##working
print("🟡 [CROSSWALK_UTILS] This is d:/Downloads/finale6/Khatam final/khatam/qt_app_pyside/utils/crosswalk_utils.py LOADED")
import cv2
import numpy as np
from sklearn import linear_model

def detect_crosswalk_and_violation_line(frame, traffic_light_position=None, debug=False):
    """
    Robust crosswalk and violation line detection for red-light violation system.
    Returns:
        frame_with_overlays, crosswalk_bbox, violation_line_y, debug_info
    """
    frame_out = frame.copy()
    h, w = frame.shape[:2]
    debug_info = {}

    # === Step 1: Robust white color mask (HSV) ===
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lower_white = np.array([0, 0, 180])
    upper_white = np.array([180, 80, 255])
    mask = cv2.inRange(hsv, lower_white, upper_white)

    # === Step 2: Morphological filtering ===
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    # === Step 3: Contour extraction and filtering ===
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    crosswalk_bars = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw > w * 0.05 and ch < h * 0.15:
            crosswalk_bars.append((x, y, cw, ch))

    # === Step 4: Draw detected bars for debug ===
    for (x, y, cw, ch) in crosswalk_bars:
        cv2.rectangle(frame_out, (x, y), (x + cw, y + ch), (0, 255, 255), 2)  # yellow

    # === Step 5: Violation line placement at bottom of bars ===
    ys = np.array([y for (x, y, w, h) in crosswalk_bars])
    hs = np.array([h for (x, y, w, h) in crosswalk_bars])
    if len(ys) >= 3:
        bottom_edges = ys + hs
        violation_line_y = int(np.max(bottom_edges)) + 5  # +5 offset
        violation_line_y = min(violation_line_y, h - 1)
        crosswalk_bbox = (0, int(np.min(ys)), w, int(np.max(bottom_edges)) - int(np.min(ys)))
        # Draw semi-transparent crosswalk region
        overlay = frame_out.copy()
        cv2.rectangle(overlay, (0, int(np.min(ys))), (w, int(np.max(bottom_edges))), (0, 255, 0), -1)
        frame_out = cv2.addWeighted(overlay, 0.2, frame_out, 0.8, 0)
        cv2.rectangle(frame_out, (0, int(np.min(ys))), (w, int(np.max(bottom_edges))), (0, 255, 0), 2)
        cv2.putText(frame_out, "Crosswalk", (10, int(np.min(ys)) - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    else:
        violation_line_y = int(h * 0.65)
        crosswalk_bbox = None

    # === Draw violation line ===
    cv2.line(frame_out, (0, violation_line_y), (w, violation_line_y), (0, 0, 255), 3)
    cv2.putText(frame_out, "Violation Line", (10, violation_line_y - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    debug_info['crosswalk_bars'] = crosswalk_bars
    debug_info['violation_line_y'] = violation_line_y
    debug_info['crosswalk_bbox'] = crosswalk_bbox

    return frame_out, crosswalk_bbox, violation_line_y, debug_info

def draw_violation_line(frame: np.ndarray, y: int, color=(0, 0, 255), thickness=4, style='solid', label='Violation Line'):
    h, w = frame.shape[:2]
    x1, x2 = 0, w
    overlay = frame.copy()
    if style == 'dashed':
        dash_len = 30
        gap = 20
        for x in range(x1, x2, dash_len + gap):
            x_end = min(x + dash_len, x2)
            cv2.line(overlay, (x, y), (x_end, y), color, thickness, lineType=cv2.LINE_AA)
    else:
        cv2.line(overlay, (x1, y), (x2, y), color, thickness, lineType=cv2.LINE_AA)
    cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)
    if label:
        font = cv2.FONT_HERSHEY_SIMPLEX
        text_size, _ = cv2.getTextSize(label, font, 0.8, 2)
        text_x = max(10, (w - text_size[0]) // 2)
        text_y = max(0, y - 12)
        cv2.rectangle(frame, (text_x - 5, text_y - text_size[1] - 5), (text_x + text_size[0] + 5, text_y + 5), (0,0,0), -1)
        cv2.putText(frame, label, (text_x, text_y), font, 0.8, color, 2, cv2.LINE_AA)
    return frame

def get_violation_line_y(frame, traffic_light_bbox=None, crosswalk_bbox=None):
    """
    Returns the y-coordinate of the violation line using the following priority:
    1. Crosswalk bbox (most accurate)
    2. Stop line detection via image processing (CV)
    3. Traffic light bbox heuristic
    4. Fallback (default)
    """
    height, width = frame.shape[:2]
    # 1. Crosswalk bbox
    if crosswalk_bbox is not None and len(crosswalk_bbox) == 4:
        return int(crosswalk_bbox[1]) - 15
    # 2. Stop line detection (CV)
    roi_height = int(height * 0.4)
    roi_y = height - roi_height
    roi = frame[roi_y:height, 0:width]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 15, -2
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 1))
    processed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    stop_line_candidates = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        aspect_ratio = w / max(h, 1)
        normalized_width = w / width
        if (aspect_ratio > 5 and normalized_width > 0.3 and h < 15 and y > roi_height * 0.5):
            abs_y = y + roi_y
            stop_line_candidates.append((abs_y, w))
    if stop_line_candidates:
        stop_line_candidates.sort(key=lambda x: x[1], reverse=True)
        return stop_line_candidates[0][0]
    # 3. Traffic light bbox heuristic
    if traffic_light_bbox is not None and len(traffic_light_bbox) == 4:
        traffic_light_bottom = traffic_light_bbox[3]
        traffic_light_height = traffic_light_bbox[3] - traffic_light_bbox[1]
        estimated_distance = min(5 * traffic_light_height, height * 0.3)
        return min(int(traffic_light_bottom + estimated_distance), height - 20)
    # 4. Fallback
    return int(height * 0.75)

# Example usage:
# bbox, vline, dbg = detect_crosswalk_and_violation_line(frame, (tl_x, tl_y), perspective_M)
