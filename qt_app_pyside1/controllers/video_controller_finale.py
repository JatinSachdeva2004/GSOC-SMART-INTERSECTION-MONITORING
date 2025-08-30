
from PySide6.QtCore import QObject, Signal, QThread, Qt, QMutex, QWaitCondition, QTimer
from PySide6.QtGui import QImage, QPixmap
import cv2
import time
import numpy as np
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional
import os
import sys
import math

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import utilities
from utils.annotation_utils import (
    draw_detections, 
    draw_performance_metrics,
    resize_frame_for_display,
    convert_cv_to_qimage,
    convert_cv_to_pixmap,
    pipeline_with_violation_line
)

# Import enhanced annotation utilities
from utils.enhanced_annotation_utils import (
    enhanced_draw_detections,
    draw_performance_overlay,
    enhanced_cv_to_qimage,
    enhanced_cv_to_pixmap
)

# Import traffic light color detection utilities
from red_light_violation_pipeline import RedLightViolationPipeline
from utils.traffic_light_utils import detect_traffic_light_color, draw_traffic_light_status, ensure_traffic_light_color
from utils.crosswalk_utils2 import detect_crosswalk_and_violation_line, draw_violation_line, get_violation_line_y
from controllers.bytetrack_tracker import ByteTrackVehicleTracker
TRAFFIC_LIGHT_CLASSES = ["traffic light", "trafficlight", "tl"]
TRAFFIC_LIGHT_NAMES = ['trafficlight', 'traffic light', 'tl', 'signal']

def normalize_class_name(class_name):
    """Normalizes class names from different models/formats to a standard name"""
    if not class_name:
        return ""
    
    name_lower = class_name.lower()
    
    # Traffic light variants
    if name_lower in ['traffic light', 'trafficlight', 'traffic_light', 'tl', 'signal']:
        return 'traffic light'
    
    # Keep specific vehicle classes (car, truck, bus) separate
    # Just normalize naming variations within each class
    if name_lower in ['car', 'auto', 'automobile']:
        return 'car'
    elif name_lower in ['truck']:
        return 'truck'
    elif name_lower in ['bus']:
        return 'bus'
    elif name_lower in ['motorcycle', 'scooter', 'motorbike', 'bike']:
        return 'motorcycle'
    
    # Person variants
    if name_lower in ['person', 'pedestrian', 'human']:
        return 'person'
    
    # Other common classes can be added here
    
    return class_name

def is_traffic_light(class_name):
    """Helper function to check if a class name is a traffic light with normalization"""
    if not class_name:
        return False
    normalized = normalize_class_name(class_name)
    return normalized == 'traffic light'

class VideoController(QObject):      
    frame_ready = Signal(object, object, dict)  # QPixmap, detections, metrics
    raw_frame_ready = Signal(np.ndarray, list, float)  # frame, detections, fps
    frame_np_ready = Signal(np.ndarray)  # Direct NumPy frame signal for display
    stats_ready = Signal(dict)  # Dictionary with stats (fps, detection_time, traffic_light)
    violation_detected = Signal(dict)  # Signal emitted when a violation is detected
    progress_ready = Signal(int, int, float)  # value, max_value, timestamp
    
    def __init__(self, model_manager=None):
        """
        Initialize video controller.
        
        Args:
            model_manager: Model manager instance for detection and violation
        """        
        super().__init__()
        print("Loaded advanced VideoController from video_controller_new.py")  # DEBUG: Confirm correct controller
        
        self._running = False
        self.source = None
        self.source_type = None
        self.source_fps = 0
        self.performance_metrics = {}
        self.mutex = QMutex()
        
        # Performance tracking
        self.processing_times = deque(maxlen=100)  # Store last 100 processing times
        self.fps_history = deque(maxlen=100)       # Store last 100 FPS values
        self.start_time = time.time()
        self.frame_count = 0
        self.actual_fps = 0.0
        
        self.model_manager = model_manager
        self.inference_model = None
        self.tracker = None
        
        self.current_frame = None
        self.current_detections = []
        
        # Traffic light state tracking
        self.latest_traffic_light = {"color": "unknown", "confidence": 0.0}
        
        # Vehicle tracking settings
        self.vehicle_history = {}  # Dictionary to store vehicle position history
        self.vehicle_statuses = {}  # Track stable movement status
        self.movement_threshold = 1.5  # ADJUSTED: More balanced movement detection (was 0.8)
        self.min_confidence_threshold = 0.3  # FIXED: Lower threshold for better detection (was 0.5)
        
        # Enhanced violation detection settings
        self.position_history_size = 20  # Increased from 10 to track longer history
        self.crossing_check_window = 8   # Check for crossings over the last 8 frames instead of just 2
        self.max_position_jump = 50      # Maximum allowed position jump between frames (detect ID switches)
        
        # Set up violation detection
        try:
            from controllers.red_light_violation_detector import RedLightViolationDetector
            self.violation_detector = RedLightViolationDetector()
            print("✅ Red light violation detector initialized")
        except Exception as e:
            self.violation_detector = None
            print(f"❌ Could not initialize violation detector: {e}")
            
        # Import crosswalk detection
        try:
            self.detect_crosswalk_and_violation_line = detect_crosswalk_and_violation_line
            # self.draw_violation_line = draw_violation_line
            print("✅ Crosswalk detection utilities imported")
        except Exception as e:
            print(f"❌ Could not import crosswalk detection: {e}")
            self.detect_crosswalk_and_violation_line = lambda frame, *args: (None, None, {})
            # self.draw_violation_line = lambda frame, *args, **kwargs: frame
        
        # Configure thread
        self.thread = QThread()
        self.moveToThread(self.thread)
        self.thread.started.connect(self._run)
          # Performance measurement
        self.mutex = QMutex()
        self.condition = QWaitCondition()
        self.performance_metrics = {
            'FPS': 0.0,
            'Detection (ms)': 0.0,
            'Total (ms)': 0.0
        }
        
        # Setup render timer with more aggressive settings for UI updates
        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self._process_frame)
        
        # Frame buffer
        self.current_frame = None
        self.current_detections = []
        self.current_violations = []
        
        # Debug counter for monitoring frame processing
        self.debug_counter = 0
        self.violation_frame_counter = 0  # Add counter for violation processing
        
        # Initialize the traffic light color detection pipeline
        self.cv_violation_pipeline = RedLightViolationPipeline(debug=True)
        
        # Initialize vehicle tracker
        self.vehicle_tracker = ByteTrackVehicleTracker()
        
        # Add red light violation system
        # self.red_light_violation_system = RedLightViolationSystem()
        
    def set_source(self, source):
        """
        Set video source (file path, camera index, or URL)
        
        Args:
            source: Video source - can be a camera index (int), file path (str), 
                   or URL (str). If None, defaults to camera 0.
                   
        Returns:
            bool: True if source was set successfully, False otherwise
        """
        print(f"🎬 VideoController.set_source called with: {source} (type: {type(source)})")
        
        # Store current state
        was_running = self._running
        
        # Stop current processing if running
        if self._running:
            print("⏹️ Stopping current video processing")
            self.stop()
        
        try:
            # Handle source based on type with better error messages
            if source is None:
                print("⚠️ Received None source, defaulting to camera 0")
                self.source = 0
                self.source_type = "camera"
                
            elif isinstance(source, str) and source.strip():
                if os.path.exists(source):
                    # Valid file path
                    self.source = source
                    self.source_type = "file"
                    print(f"📄 Source set to file: {self.source}")
                elif source.lower().startswith(("http://", "https://", "rtsp://", "rtmp://")):
                    # URL stream
                    self.source = source
                    self.source_type = "url"
                    print(f"🌐 Source set to URL stream: {self.source}")
                elif source.isdigit():
                    # String camera index (convert to int)
                    self.source = int(source)
                    self.source_type = "camera"
                    print(f"📹 Source set to camera index: {self.source}")
                else:
                    # Try as device path or special string
                    self.source = source
                    self.source_type = "device"
                    print(f"📱 Source set to device path: {self.source}")
                    
            elif isinstance(source, int):
                # Camera index
                self.source = source
                self.source_type = "camera"
                print(f"📹 Source set to camera index: {self.source}")
                
            else:
                # Unrecognized - default to camera 0 with warning
                print(f"⚠️ Unrecognized source type: {type(source)}, defaulting to camera 0")
                self.source = 0
                self.source_type = "camera"
        except Exception as e:
            print(f"❌ Error setting source: {e}")
            self.source = 0
            self.source_type = "camera"
            return False
        
        # Get properties of the source (fps, dimensions, etc)
        print(f"🔍 Getting properties for source: {self.source}")
        success = self._get_source_properties()
        
        if success:
            print(f"✅ Successfully configured source: {self.source} ({self.source_type})")
            
            # Reset ByteTrack tracker for new source to ensure IDs start from 1
            if hasattr(self, 'vehicle_tracker') and self.vehicle_tracker is not None:
                try:
                    print("🔄 Resetting vehicle tracker for new source")
                    self.vehicle_tracker.reset()
                except Exception as e:
                    print(f"⚠️ Could not reset vehicle tracker: {e}")
            
            # Emit successful source change
            self.stats_ready.emit({
                'source_changed': True,
                'source_type': self.source_type,
                'fps': self.source_fps if hasattr(self, 'source_fps') else 0,
                'dimensions': f"{self.frame_width}x{self.frame_height}" if hasattr(self, 'frame_width') else "unknown"
            })
            
            # Restart if previously running
            if was_running:
                print("▶️ Restarting video processing with new source")
                self.start()
        else:
            print(f"❌ Failed to configure source: {self.source}")
            # Notify UI about the error
            self.stats_ready.emit({
                'source_changed': False,
                'error': f"Invalid video source: {self.source}",
                'source_type': self.source_type,
                'fps': 0,
                'detection_time_ms': "0",
                'traffic_light_color': {"color": "unknown", "confidence": 0.0}
            })
            
            return False
            
        # Return success status
        return success
    
    def _get_source_properties(self):
        """
        Get properties of video source
        
        Returns:
            bool: True if source was successfully opened, False otherwise
        """
        try:
            print(f"🔍 Opening video source for properties check: {self.source}")
            cap = cv2.VideoCapture(self.source)
            
            # Verify capture opened successfully
            if not cap.isOpened():
                print(f"❌ Failed to open video source: {self.source}")
                return False
                
            # Read properties
            self.source_fps = cap.get(cv2.CAP_PROP_FPS)
            if self.source_fps <= 0:
                print("⚠️ Source FPS not available, using default 30 FPS")
                self.source_fps = 30.0  # Default if undetectable
            
            self.frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))                
            self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            
            # Try reading a test frame to confirm source is truly working
            ret, test_frame = cap.read()
            if not ret or test_frame is None:
                print("⚠️ Could not read test frame from source")
                # For camera sources, try one more time with delay
                if self.source_type == "camera":
                    print("🔄 Retrying camera initialization...")
                    time.sleep(1.0)  # Wait a moment for camera to initialize
                    ret, test_frame = cap.read()
                    if not ret or test_frame is None:
                        print("❌ Camera initialization failed after retry")
                        cap.release()
                        return False
                else:
                    print("❌ Could not read frames from video source")
                    cap.release()
                    return False
                
            # Release the capture
            cap.release()
            
            print(f"✅ Video source properties: {self.frame_width}x{self.frame_height}, {self.source_fps} FPS")
            return True
            
        except Exception as e:
            print(f"❌ Error getting source properties: {e}")
            return False
            return False
            
    def start(self):
        """Start video processing"""
        if not self._running:
            self._running = True
            self.start_time = time.time()
            self.frame_count = 0
            self.debug_counter = 0
            print("DEBUG: Starting video processing thread")
            
            # Reset ByteTrack tracker to ensure IDs start from 1
            if hasattr(self, 'vehicle_tracker') and self.vehicle_tracker is not None:
                try:
                    print("🔄 Resetting vehicle tracker for new session")
                    self.vehicle_tracker.reset()
                except Exception as e:
                    print(f"⚠️ Could not reset vehicle tracker: {e}")
            
            # Start the processing thread - add more detailed debugging
            if not self.thread.isRunning():
                print("🚀 Thread not running, starting now...")
                try:
                    self.thread.start()
                    print("✅ Thread started successfully")
                    print(f"🔄 Thread state: running={self.thread.isRunning()}, finished={self.thread.isFinished()}")
                except Exception as e:
                    print(f"❌ Failed to start thread: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print("⚠️ Thread is already running!")
                print(f"🔄 Thread state: running={self.thread.isRunning()}, finished={self.thread.isFinished()}")
            
            # Start the render timer with a very aggressive interval (10ms = 100fps)
            # This ensures we can process frames as quickly as possible
            print("⏱️ Starting render timer...")
            self.render_timer.start(10)
            print("✅ Render timer started at 100Hz")
    
    def stop(self):
        """Stop video processing"""
        if self._running:
            print("DEBUG: Stopping video processing")
            self._running = False
            self.render_timer.stop()
            # Properly terminate the thread
            if self.thread.isRunning():
                self.thread.quit()
                if not self.thread.wait(3000):  # Wait 3 seconds max
                    self.thread.terminate()
                    print("WARNING: Thread termination forced")
            # Clear the current frame
            self.mutex.lock()
            self.current_frame = None
            self.mutex.unlock()
            print("DEBUG: Video processing stopped")

    def __del__(self):
        print("[VideoController] __del__ called. Cleaning up thread and timer.")
        self.stop()
        if self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(1000)
        self.render_timer.stop()
    
    def capture_snapshot(self) -> np.ndarray:
        """Capture current frame"""
        if self.current_frame is not None:
            return self.current_frame.copy()
        return None
        
    def _run(self):
        """Main processing loop (runs in thread)"""
        try:
            # Print the source we're trying to open
            print(f"DEBUG: Opening video source: {self.source} (type: {type(self.source)})")
            
            cap = None  # Initialize capture variable
            
            # Try to open source with more robust error handling
            max_retries = 3
            retry_delay = 1.0  # seconds
            
            # Function to attempt opening the source with multiple retries
            def try_open_source(src, retries=max_retries, delay=retry_delay):
                for attempt in range(1, retries + 1):
                    print(f"🎥 Opening source (attempt {attempt}/{retries}): {src}")
                    try:
                        capture = cv2.VideoCapture(src)
                        if capture.isOpened():
                            # Try to read a test frame to confirm it's working
                            ret, test_frame = capture.read()
                            if ret and test_frame is not None:
                                print(f"✅ Source opened successfully: {src}")
                                # Reset capture position for file sources
                                if isinstance(src, str) and os.path.exists(src):
                                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                return capture
                            else:
                                print(f"⚠️ Source opened but couldn't read frame: {src}")
                                capture.release()
                        else:
                            print(f"⚠️ Failed to open source: {src}")
                            
                        # Retry after delay
                        if attempt < retries:
                            print(f"Retrying in {delay:.1f} seconds...")
                            time.sleep(delay)
                    except Exception as e:
                        print(f"❌ Error opening source {src}: {e}")
                        if attempt < retries:
                            print(f"Retrying in {delay:.1f} seconds...")
                            time.sleep(delay)
                
                print(f"❌ Failed to open source after {retries} attempts: {src}")
                return None
            
            # Handle different source types
            if isinstance(self.source, str) and os.path.exists(self.source):
                # It's a valid file path
                print(f"📄 Opening video file: {self.source}")
                cap = try_open_source(self.source)
                
            elif isinstance(self.source, int) or (isinstance(self.source, str) and self.source.isdigit()):
                # It's a camera index
                camera_idx = int(self.source) if isinstance(self.source, str) else self.source
                print(f"📹 Opening camera with index: {camera_idx}")
                
                # For cameras, try with different backend options if it fails
                cap = try_open_source(camera_idx)
                
                # If failed, try with DirectShow backend on Windows
                if cap is None and os.name == 'nt':
                    print("🔄 Trying camera with DirectShow backend...")
                    cap = try_open_source(camera_idx + cv2.CAP_DSHOW)
                    
            else:
                # Try as a string source (URL or device path)
                print(f"🌐 Opening source as string: {self.source}")
                cap = try_open_source(str(self.source))
                
            # Check if we successfully opened the source
            if cap is None:
                print(f"❌ Failed to open video source after all attempts: {self.source}")
                # Notify UI about the error
                self.stats_ready.emit({
                    'error': f"Could not open video source: {self.source}",
                    'fps': "0",
                    'detection_time_ms': "0",
                    'traffic_light_color': {"color": "unknown", "confidence": 0.0}
                })
                return
                    
            # Check again to ensure capture is valid
            if not cap or not cap.isOpened():
                print(f"ERROR: Could not open video source {self.source}")
                # Emit a signal to notify UI about the error
                self.stats_ready.emit({
                    'error': f"Failed to open video source: {self.source}",
                    'fps': "0",
                    'detection_time_ms': "0",
                    'traffic_light_color': {"color": "unknown", "confidence": 0.0}
                })
                return
                
            # Configure frame timing based on source FPS
            frame_time = 1.0 / self.source_fps if self.source_fps > 0 else 0.033
            prev_time = time.time()
            
            # Log successful opening
            print(f"SUCCESS: Video source opened: {self.source}")
            print(f"Source info - FPS: {self.source_fps}, Size: {self.frame_width}x{self.frame_height}")
              # Main processing loop
            frame_error_count = 0
            max_consecutive_errors = 10
            
            while self._running and cap.isOpened():
                try:
                    ret, frame = cap.read()
                    # Add critical frame debugging
                    print(f"🟡 Frame read attempt: ret={ret}, frame={None if frame is None else frame.shape}")
                    
                    if not ret or frame is None:
                        frame_error_count += 1
                        print(f"⚠️ Frame read error ({frame_error_count}/{max_consecutive_errors})")
                        
                        if frame_error_count >= max_consecutive_errors:
                            print("❌ Too many consecutive frame errors, stopping video thread")
                            break
                            
                        # Skip this iteration and try again
                        time.sleep(0.1)  # Wait a bit before trying again
                        continue
                    
                    # Reset the error counter if we successfully got a frame
                    frame_error_count = 0
                except Exception as e:
                    print(f"❌ Critical error reading frame: {e}")
                    frame_error_count += 1
                    if frame_error_count >= max_consecutive_errors:
                        print("❌ Too many errors, stopping video thread")
                        break
                    continue
                    
                # Detection and violation processing
                process_start = time.time()
                
                # Process detections
                detection_start = time.time()
                detections = []
                if self.model_manager:
                    detections = self.model_manager.detect(frame)
                    
                    # Normalize class names for consistency and check for traffic lights
                    traffic_light_indices = []
                    for i, det in enumerate(detections):
                        if 'class_name' in det:
                            original_name = det['class_name']
                            normalized_name = normalize_class_name(original_name)
                            
                            # Keep track of traffic light indices
                            if normalized_name == 'traffic light' or original_name == 'traffic light':
                                traffic_light_indices.append(i)
                                
                            if original_name != normalized_name:
                                print(f"📊 Normalized class name: '{original_name}' -> '{normalized_name}'")
                                
                            det['class_name'] = normalized_name
                            
                    # Ensure we have at least one traffic light for debugging
                    if not traffic_light_indices and self.source_type == 'video':
                        print("⚠️ No traffic lights detected, checking for objects that might be traffic lights...")
                        
                        # Try lowering the confidence threshold specifically for traffic lights
                        # This is only for debugging purposes
                        if self.model_manager and hasattr(self.model_manager, 'detect'):
                            try:
                                low_conf_detections = self.model_manager.detect(frame, conf_threshold=0.2)
                                for det in low_conf_detections:
                                    if 'class_name' in det and det['class_name'] == 'traffic light':
                                        if det not in detections:
                                            print(f"🚦 Found low confidence traffic light: {det['confidence']:.2f}")
                                            detections.append(det)
                            except:
                                pass
                            
                detection_time = (time.time() - detection_start) * 1000
                
                # Violation detection is disabled
                violation_start = time.time()
                violations = []
                # if self.model_manager and detections:
                #     violations = self.model_manager.detect_violations(
                #         detections, frame, time.time()
                #     )
                violation_time = (time.time() - violation_start) * 1000
                
                # Update tracking if available
                if self.model_manager:
                    detections = self.model_manager.update_tracking(detections, frame)
                    # If detections are returned as tuples, convert to dicts for downstream code
                    if detections and isinstance(detections[0], tuple):
                        # Convert (id, bbox, conf, class_id) to dict
                        detections = [
                            {'id': d[0], 'bbox': d[1], 'confidence': d[2], 'class_id': d[3]}
                            for d in detections
                        ]
                
                # Calculate timing metrics
                process_time = (time.time() - process_start) * 1000
                self.processing_times.append(process_time)
                
                # Update FPS
                now = time.time()
                self.frame_count += 1
                elapsed = now - self.start_time
                if elapsed > 0:
                    self.actual_fps = self.frame_count / elapsed
                    
                fps_smoothed = 1.0 / (now - prev_time) if now > prev_time else 0
                prev_time = now
                  # Update metrics
                self.performance_metrics = {
                    'FPS': f"{fps_smoothed:.1f}",
                    'Detection (ms)': f"{detection_time:.1f}",
                    'Total (ms)': f"{process_time:.1f}"
                }
                
                # Store current frame data (thread-safe)
                self.mutex.lock()
                self.current_frame = frame.copy()
                self.current_detections = detections
                self.mutex.unlock()
                  # Process frame with annotations before sending to UI
                annotated_frame = frame.copy()
                
                # --- VIOLATION DETECTION LOGIC (Run BEFORE drawing boxes) ---
                # First get violation information so we can color boxes appropriately
                violating_vehicle_ids = set()  # Track which vehicles are violating
                violations = []
                
                # Initialize traffic light variables
                traffic_lights = []
                has_traffic_lights = False
                
                # Handle multiple traffic lights with consensus approach
                traffic_light_count = 0
                for det in detections:
                    if is_traffic_light(det.get('class_name')):
                        has_traffic_lights = True
                        traffic_light_count += 1
                        if 'traffic_light_color' in det:
                            light_info = det['traffic_light_color']
                            traffic_lights.append({'bbox': det['bbox'], 'color': light_info.get('color', 'unknown'), 'confidence': light_info.get('confidence', 0.0)})
                
                print(f"[TRAFFIC LIGHT] Detected {traffic_light_count} traffic light(s), has_traffic_lights={has_traffic_lights}")
                if has_traffic_lights:
                    print(f"[TRAFFIC LIGHT] Traffic light colors: {[tl.get('color', 'unknown') for tl in traffic_lights]}")
                
                # Get traffic light position for crosswalk detection
                traffic_light_position = None
                if has_traffic_lights:
                    for det in detections:
                        if is_traffic_light(det.get('class_name')) and 'bbox' in det:
                            traffic_light_bbox = det['bbox']
                            # Extract center point from bbox for crosswalk utils
                            x1, y1, x2, y2 = traffic_light_bbox
                            traffic_light_position = ((x1 + x2) // 2, (y1 + y2) // 2)
                            break

                # Run crosswalk detection ONLY if traffic light is detected
                crosswalk_bbox, violation_line_y, debug_info = None, None, {}
                if has_traffic_lights and traffic_light_position is not None:
                    try:
                        print(f"[CROSSWALK] Traffic light detected at {traffic_light_position}, running crosswalk detection")
                        # Use new crosswalk_utils2 logic only when traffic light exists
                        annotated_frame, crosswalk_bbox, violation_line_y, debug_info = detect_crosswalk_and_violation_line(
                            annotated_frame,
                            traffic_light_position=traffic_light_position
                        )
                        print(f"[CROSSWALK] Detection result: crosswalk_bbox={crosswalk_bbox is not None}, violation_line_y={violation_line_y}")
                        # --- Draw crosswalk region if detected and close to traffic light ---
                        # (REMOVED: Do not draw crosswalk box or label)
                        # if crosswalk_bbox is not None:
                        #     x, y, w, h = map(int, crosswalk_bbox)
                        #     tl_x, tl_y = traffic_light_position
                        #     crosswalk_center_y = y + h // 2
                        #     distance = abs(crosswalk_center_y - tl_y)
                        #     print(f"[CROSSWALK DEBUG] Crosswalk bbox: {crosswalk_bbox}, Traffic light: {traffic_light_position}, vertical distance: {distance}")
                        #     if distance < 120:
                        #         cv2.rectangle(annotated_frame, (x, y), (x + w, y + h), (0, 255, 0), 3)
                        #         cv2.putText(annotated_frame, "Crosswalk", (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                        #     # Top and bottom edge of crosswalk
                        #     top_edge = y
                        #     bottom_edge = y + h
                        #     if abs(tl_y - top_edge) < abs(tl_y - bottom_edge):
                        #         crosswalk_edge_y = top_edge
                        #     else:
                        #         crosswalk_edge_y = bottom_edge
                        if crosswalk_bbox is not None:
                            x, y, w, h = map(int, crosswalk_bbox)
                            tl_x, tl_y = traffic_light_position
                            crosswalk_center_y = y + h // 2
                            distance = abs(crosswalk_center_y - tl_y)
                            print(f"[CROSSWALK DEBUG] Crosswalk bbox: {crosswalk_bbox}, Traffic light: {traffic_light_position}, vertical distance: {distance}")
                            # Top and bottom edge of crosswalk
                            top_edge = y
                            bottom_edge = y + h
                            if abs(tl_y - top_edge) < abs(tl_y - bottom_edge):
                                crosswalk_edge_y = top_edge
                            else:
                                crosswalk_edge_y = bottom_edge
                    except Exception as e:
                        print(f"[ERROR] Crosswalk detection failed: {e}")
                        crosswalk_bbox, violation_line_y, debug_info = None, None, {}
                else:
                    print(f"[CROSSWALK] No traffic light detected (has_traffic_lights={has_traffic_lights}), skipping crosswalk detection")
                    # NO crosswalk detection without traffic light
                    violation_line_y = None
                
                # Check if crosswalk is detected
                crosswalk_detected = crosswalk_bbox is not None
                stop_line_detected = debug_info.get('stop_line') is not None
                
                # ALWAYS process vehicle tracking (moved outside violation logic)
                tracked_vehicles = []
                if hasattr(self, 'vehicle_tracker') and self.vehicle_tracker is not None:
                    try:
                        # Filter vehicle detections
                        vehicle_classes = ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']
                        vehicle_dets = []
                        h, w = frame.shape[:2]
                        
                        print(f"[TRACK DEBUG] Processing {len(detections)} total detections")
                        
                        for det in detections:
                            if (det.get('class_name') in vehicle_classes and 
                                'bbox' in det and 
                                det.get('confidence', 0) > self.min_confidence_threshold):
                                
                                # Check bbox dimensions
                                bbox = det['bbox']
                                x1, y1, x2, y2 = bbox
                                box_w, box_h = x2-x1, y2-y1
                                box_area = box_w * box_h
                                area_ratio = box_area / (w * h)
                                
                                print(f"[TRACK DEBUG] Vehicle {det.get('class_name')} conf={det.get('confidence'):.2f}, area_ratio={area_ratio:.4f}")
                                
                                if 0.001 <= area_ratio <= 0.25:
                                    vehicle_dets.append(det)
                                    print(f"[TRACK DEBUG] Added vehicle: {det.get('class_name')} conf={det.get('confidence'):.2f}")
                                else:
                                    print(f"[TRACK DEBUG] Rejected vehicle: area_ratio={area_ratio:.4f} not in range [0.001, 0.25]")
                        
                        print(f"[TRACK DEBUG] Filtered to {len(vehicle_dets)} vehicle detections")
                        
                        # Update tracker
                        if len(vehicle_dets) > 0:
                            print(f"[TRACK DEBUG] Updating tracker with {len(vehicle_dets)} vehicles...")
                            tracks = self.vehicle_tracker.update(vehicle_dets, frame)
                            # Filter out tracks without bbox to avoid warnings
                            valid_tracks = []
                            for track in tracks:
                                bbox = None
                                if isinstance(track, dict):
                                    bbox = track.get('bbox', None)
                                else:
                                    bbox = getattr(track, 'bbox', None)
                                if bbox is not None:
                                    valid_tracks.append(track)
                                else:
                                    print(f"Warning: Track has no bbox, skipping: {track}")
                            tracks = valid_tracks
                            print(f"[TRACK DEBUG] Tracker returned {len(tracks)} tracks (after bbox filter)")
                        else:
                            print(f"[TRACK DEBUG] No vehicles to track, skipping tracker update")
                            tracks = []
                        
                        # Process each tracked vehicle
                        tracked_vehicles = []
                        track_ids_seen = []
                        
                        for track in tracks:
                            track_id = track['id']
                            bbox = track['bbox']
                            x1, y1, x2, y2 = map(float, bbox)
                            center_y = (y1 + y2) / 2
                            
                            # Check for duplicate IDs
                            if track_id in track_ids_seen:
                                print(f"[TRACK ERROR] Duplicate ID detected: {track_id}")
                            track_ids_seen.append(track_id)
                            
                            print(f"[TRACK DEBUG] Processing track ID={track_id} bbox={bbox}")
                            
                            # Initialize or update vehicle history
                            if track_id not in self.vehicle_history:
                                from collections import deque
                                self.vehicle_history[track_id] = deque(maxlen=self.position_history_size)
                            
                            # Initialize vehicle status if not exists
                            if track_id not in self.vehicle_statuses:
                                self.vehicle_statuses[track_id] = {
                                    'recent_movement': [],
                                    'violation_history': [],
                                    'crossed_during_red': False,
                                    'last_position': None,  # Track last position for jump detection
                                    'suspicious_jumps': 0   # Count suspicious position jumps
                                }
                            
                            # Detect suspicious position jumps (potential ID switches)
                            if self.vehicle_statuses[track_id]['last_position'] is not None:
                                last_y = self.vehicle_statuses[track_id]['last_position']
                                center_y = (y1 + y2) / 2
                                position_jump = abs(center_y - last_y)
                                
                                if position_jump > self.max_position_jump:
                                    self.vehicle_statuses[track_id]['suspicious_jumps'] += 1
                                    print(f"[TRACK WARNING] Vehicle ID={track_id} suspicious position jump: {last_y:.1f} -> {center_y:.1f} (jump={position_jump:.1f})")
                                    
                                    # If too many suspicious jumps, reset violation status to be safe
                                    if self.vehicle_statuses[track_id]['suspicious_jumps'] > 2:
                                        print(f"[TRACK RESET] Vehicle ID={track_id} has too many suspicious jumps, resetting violation status")
                                        self.vehicle_statuses[track_id]['crossed_during_red'] = False
                                        self.vehicle_statuses[track_id]['suspicious_jumps'] = 0
                            
                            # Update position history and last position
                            self.vehicle_history[track_id].append(center_y)
                            self.vehicle_statuses[track_id]['last_position'] = center_y
                            
                            # BALANCED movement detection - detect clear movement while avoiding false positives
                            is_moving = False
                            movement_detected = False
                            
                            if len(self.vehicle_history[track_id]) >= 3:  # Require at least 3 frames for movement detection
                                recent_positions = list(self.vehicle_history[track_id])
                                
                                # Check movement over 3 frames for quick response
                                if len(recent_positions) >= 3:
                                    movement_3frames = abs(recent_positions[-1] - recent_positions[-3])
                                    if movement_3frames > self.movement_threshold:  # More responsive threshold
                                        movement_detected = True
                                        print(f"[MOVEMENT] Vehicle ID={track_id} MOVING: 3-frame movement = {movement_3frames:.1f}")
                                
                                # Confirm with longer movement for stability (if available)
                                if len(recent_positions) >= 5:
                                    movement_5frames = abs(recent_positions[-1] - recent_positions[-5])
                                    if movement_5frames > self.movement_threshold * 1.5:  # Moderate threshold for 5 frames
                                        movement_detected = True
                                        print(f"[MOVEMENT] Vehicle ID={track_id} MOVING: 5-frame movement = {movement_5frames:.1f}")
                            
                            # Store historical movement for smoothing - require consistent movement
                            self.vehicle_statuses[track_id]['recent_movement'].append(movement_detected)
                            if len(self.vehicle_statuses[track_id]['recent_movement']) > 4:  # Shorter history for quicker response
                                self.vehicle_statuses[track_id]['recent_movement'].pop(0)
                            
                            # BALANCED: Require majority of recent frames to show movement (2 out of 4)
                            recent_movement_count = sum(self.vehicle_statuses[track_id]['recent_movement'])
                            total_recent_frames = len(self.vehicle_statuses[track_id]['recent_movement'])
                            if total_recent_frames >= 2 and recent_movement_count >= (total_recent_frames * 0.5):  # 50% of frames must show movement
                                is_moving = True
                            
                            print(f"[TRACK DEBUG] Vehicle ID={track_id} is_moving={is_moving} (threshold={self.movement_threshold})")
                            
                            # Initialize as not violating
                            is_violation = False
                            
                            tracked_vehicles.append({
                                'id': track_id,
                                'bbox': bbox,
                                'center_y': center_y,
                                'is_moving': is_moving,
                                'is_violation': is_violation
                            })
                        
                        print(f"[DEBUG] ByteTrack tracked {len(tracked_vehicles)} vehicles")
                        for i, tracked in enumerate(tracked_vehicles):
                            print(f"  Vehicle {i}: ID={tracked['id']}, center_y={tracked['center_y']:.1f}, moving={tracked['is_moving']}, violating={tracked['is_violation']}")
                        
                        # DEBUG: Print all tracked vehicle IDs and their bboxes for this frame
                        if tracked_vehicles:
                            print(f"[DEBUG] All tracked vehicles this frame:")
                            for v in tracked_vehicles:
                                print(f"    ID={v['id']} bbox={v['bbox']} center_y={v.get('center_y', 'NA')}")
                        else:
                            print("[DEBUG] No tracked vehicles this frame!")
                        
                        # Clean up old vehicle data
                        current_track_ids = [tracked['id'] for tracked in tracked_vehicles]
                        self._cleanup_old_vehicle_data(current_track_ids)
                        
                    except Exception as e:
                        print(f"[ERROR] Vehicle tracking failed: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    print("[WARN] ByteTrack vehicle tracker not available!")
                
                # Process violations - CHECK VEHICLES THAT CROSS THE LINE OVER A WINDOW OF FRAMES
                # IMPORTANT: Only process violations if traffic light is detected AND violation line exists
                if has_traffic_lights and violation_line_y is not None and tracked_vehicles:
                    print(f"[VIOLATION DEBUG] Traffic light present, checking {len(tracked_vehicles)} vehicles against violation line at y={violation_line_y}")
                    
                    # Check each tracked vehicle for violations
                    for tracked in tracked_vehicles:
                        track_id = tracked['id']
                        center_y = tracked['center_y']
                        is_moving = tracked['is_moving']
                        
                        # Get position history for this vehicle
                        position_history = list(self.vehicle_history[track_id])
                        
                        # Enhanced crossing detection: check over a window of frames
                        line_crossed_in_window = False
                        crossing_details = None
                        
                        if len(position_history) >= 2:
                            # Check for crossing over the last N frames (configurable window)
                            window_size = min(self.crossing_check_window, len(position_history))
                            
                            for i in range(1, window_size):
                                prev_y = position_history[-(i+1)]  # Earlier position
                                curr_y = position_history[-i]     # Later position
                                
                                # Check if vehicle crossed the line in this frame pair
                                if prev_y < violation_line_y and curr_y >= violation_line_y:
                                    line_crossed_in_window = True
                                    crossing_details = {
                                        'frames_ago': i,
                                        'prev_y': prev_y,
                                        'curr_y': curr_y,
                                        'window_checked': window_size
                                    }
                                    print(f"[VIOLATION DEBUG] Vehicle ID={track_id} crossed line {i} frames ago: {prev_y:.1f} -> {curr_y:.1f}")
                                    break
                        
                        # Check if traffic light is red
                        is_red_light = self.latest_traffic_light and self.latest_traffic_light.get('color') == 'red'
                        
                        print(f"[VIOLATION DEBUG] Vehicle ID={track_id}: latest_traffic_light={self.latest_traffic_light}, is_red_light={is_red_light}")
                        print(f"[VIOLATION DEBUG] Vehicle ID={track_id}: position_history={[f'{p:.1f}' for p in position_history[-5:]]}");  # Show last 5 positions
                        print(f"[VIOLATION DEBUG] Vehicle ID={track_id}: line_crossed_in_window={line_crossed_in_window}, crossing_details={crossing_details}")
                        
                        # Enhanced violation detection: vehicle crossed the line while moving and light is red
                        actively_crossing = (line_crossed_in_window and is_moving and is_red_light)
                        
                        # Initialize violation status for new vehicles
                        if 'crossed_during_red' not in self.vehicle_statuses[track_id]:
                            self.vehicle_statuses[track_id]['crossed_during_red'] = False
                        
                        # Mark vehicle as having crossed during red if it actively crosses
                        if actively_crossing:
                            # Additional validation: ensure it's not a false positive from ID switch
                            suspicious_jumps = self.vehicle_statuses[track_id].get('suspicious_jumps', 0)
                            if suspicious_jumps <= 1:  # Allow crossing if not too many suspicious jumps
                                self.vehicle_statuses[track_id]['crossed_during_red'] = True
                                print(f"[VIOLATION ALERT] Vehicle ID={track_id} CROSSED line during red light!")
                                print(f"  -> Crossing details: {crossing_details}")
                            else:
                                print(f"[VIOLATION IGNORED] Vehicle ID={track_id} crossing ignored due to {suspicious_jumps} suspicious jumps")
                        
                        # IMPORTANT: Reset violation status when light turns green (regardless of position)
                        if not is_red_light:
                            if self.vehicle_statuses[track_id]['crossed_during_red']:
                                print(f"[VIOLATION RESET] Vehicle ID={track_id} violation status reset (light turned green)")
                            self.vehicle_statuses[track_id]['crossed_during_red'] = False
                        
                        # Vehicle is violating ONLY if it crossed during red and light is still red
                        is_violation = (self.vehicle_statuses[track_id]['crossed_during_red'] and is_red_light)
                        
                        # Track current violation state for analytics - only actual crossings
                        self.vehicle_statuses[track_id]['violation_history'].append(actively_crossing)
                        if len(self.vehicle_statuses[track_id]['violation_history']) > 5:
                            self.vehicle_statuses[track_id]['violation_history'].pop(0)
                        
                        print(f"[VIOLATION DEBUG] Vehicle ID={track_id}: center_y={center_y:.1f}, line={violation_line_y}")
                        print(f"  history_window={[f'{p:.1f}' for p in position_history[-self.crossing_check_window:]]}")
                        print(f"  moving={is_moving}, red_light={is_red_light}")
                        print(f"  actively_crossing={actively_crossing}, crossed_during_red={self.vehicle_statuses[track_id]['crossed_during_red']}")
                        print(f"  suspicious_jumps={self.vehicle_statuses[track_id].get('suspicious_jumps', 0)}")
                        print(f"  FINAL_VIOLATION={is_violation}")
                        
                        # Update violation status
                        tracked['is_violation'] = is_violation
                        
                        if actively_crossing and self.vehicle_statuses[track_id].get('suspicious_jumps', 0) <= 1:  # Only add if not too many suspicious jumps
                            # Add to violating vehicles set
                            violating_vehicle_ids.add(track_id)
                            
                            # Add to violations list
                            timestamp = datetime.now()  # Keep as datetime object, not string
                            violations.append({
                                'track_id': track_id,
                                'id': track_id,
                                'bbox': [int(tracked['bbox'][0]), int(tracked['bbox'][1]), int(tracked['bbox'][2]), int(tracked['bbox'][3])],
                                'violation': 'line_crossing',
                                'violation_type': 'line_crossing',  # Add this for analytics compatibility
                                'timestamp': timestamp,
                                'line_position': violation_line_y,
                                'movement': crossing_details if crossing_details else {'prev_y': center_y, 'current_y': center_y},
                                'crossing_window': self.crossing_check_window,
                                'position_history': list(position_history[-10:])  # Include recent history for debugging
                            })
                            
                            print(f"[DEBUG] 🚨 VIOLATION DETECTED: Vehicle ID={track_id} CROSSED VIOLATION LINE")
                            print(f"    Enhanced detection: {crossing_details}")
                            print(f"    Position history: {[f'{p:.1f}' for p in position_history[-10:]]}")
                            print(f"    Detection window: {self.crossing_check_window} frames")
                            print(f"    while RED LIGHT & MOVING")
                
                # Emit progress signal after processing each frame
                if hasattr(self, 'progress_ready'):
                    self.progress_ready.emit(int(cap.get(cv2.CAP_PROP_POS_FRAMES)), int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), time.time())
                
                # Draw detections with bounding boxes - NOW with violation info
                # Only show traffic light and vehicle classes
                allowed_classes = ['traffic light', 'car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']
                filtered_detections = [det for det in detections if det.get('class_name') in allowed_classes]
                print(f"Drawing {len(filtered_detections)} detection boxes on frame (filtered)")
                
                # Statistics for debugging (always define, even if no detections)
                vehicles_with_ids = 0
                vehicles_without_ids = 0
                vehicles_moving = 0
                vehicles_violating = 0

                if detections and len(detections) > 0:
                    # Only show traffic light and vehicle classes
                    allowed_classes = ['traffic light', 'car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']
                    filtered_detections = [det for det in detections if det.get('class_name') in allowed_classes]
                    print(f"Drawing {len(filtered_detections)} detection boxes on frame (filtered)")
                    
                    # Statistics for debugging
                    vehicles_with_ids = 0
                    vehicles_without_ids = 0
                    vehicles_moving = 0
                    vehicles_violating = 0
                    
                    for det in filtered_detections:
                        if 'bbox' in det:
                            bbox = det['bbox']
                            x1, y1, x2, y2 = map(int, bbox)
                            label = det.get('class_name', 'object')
                            confidence = det.get('confidence', 0.0)
                            
                            # Robustness: ensure label and confidence are not None
                            if label is None:
                                label = 'object'
                            if confidence is None:
                                confidence = 0.0
                            class_id = det.get('class_id', -1)
                            
                            # Check if this detection corresponds to a violating or moving vehicle
                            det_center_x = (x1 + x2) / 2
                            det_center_y = (y1 + y2) / 2
                            is_violating_vehicle = False
                            is_moving_vehicle = False
                            vehicle_id = None
                            
                            # Match detection with tracked vehicles - IMPROVED MATCHING
                            if label in ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle'] and len(tracked_vehicles) > 0:
                                print(f"[MATCH DEBUG] Attempting to match {label} detection at ({det_center_x:.1f}, {det_center_y:.1f}) with {len(tracked_vehicles)} tracked vehicles")
                                best_match = None
                                best_distance = float('inf')
                                best_iou = 0.0
                                
                                for i, tracked in enumerate(tracked_vehicles):
                                    track_bbox = tracked['bbox']
                                    track_x1, track_y1, track_x2, track_y2 = map(float, track_bbox)
                                    
                                    # Calculate center distance
                                    track_center_x = (track_x1 + track_x2) / 2
                                    track_center_y = (track_y1 + track_y2) / 2
                                    center_distance = ((det_center_x - track_center_x)**2 + (det_center_y - track_center_y)**2)**0.5
                                    
                                    # Calculate IoU (Intersection over Union)
                                    intersection_x1 = max(x1, track_x1)
                                    intersection_y1 = max(y1, track_y1)
                                    intersection_x2 = min(x2, track_x2)
                                    intersection_y2 = min(y2, track_y2)
                                    
                                    if intersection_x2 > intersection_x1 and intersection_y2 > intersection_y1:
                                        intersection_area = (intersection_x2 - intersection_x1) * (intersection_y2 - intersection_y1)
                                        det_area = (x2 - x1) * (y2 - y1)
                                        track_area = (track_x2 - track_x1) * (track_y2 - track_y1)
                                        union_area = det_area + track_area - intersection_area
                                        iou = intersection_area / union_area if union_area > 0 else 0
                                    else:
                                        iou = 0
                                    
                                    print(f"[MATCH DEBUG] Track {i}: ID={tracked['id']}, center=({track_center_x:.1f}, {track_center_y:.1f}), distance={center_distance:.1f}, IoU={iou:.3f}")
                                    
                                    # Use stricter matching criteria - prioritize IoU over distance
                                    # Good match if: high IoU OR close center distance with some overlap
                                    is_good_match = (iou > 0.3) or (center_distance < 60 and iou > 0.1)
                                    
                                    if is_good_match:
                                        print(f"[MATCH DEBUG] Track {i} is a good match (IoU={iou:.3f}, distance={center_distance:.1f})")
                                        # Prefer higher IoU, then lower distance
                                        match_score = iou + (100 - min(center_distance, 100)) / 100  # Composite score
                                        if iou > best_iou or (iou == best_iou and center_distance < best_distance):
                                            best_distance = center_distance
                                            best_iou = iou
                                            best_match = tracked
                                    else:
                                        print(f"[MATCH DEBUG] Track {i} failed matching criteria (IoU={iou:.3f}, distance={center_distance:.1f})")
                                
                                if best_match:
                                    vehicle_id = best_match['id']
                                    is_moving_vehicle = best_match.get('is_moving', False)
                                    is_violating_vehicle = best_match.get('is_violation', False)
                                    print(f"[MATCH SUCCESS] Detection at ({det_center_x:.1f},{det_center_y:.1f}) matched with track ID={vehicle_id}")
                                    print(f"  -> STATUS: moving={is_moving_vehicle}, violating={is_violating_vehicle}, IoU={best_iou:.3f}, distance={best_distance:.1f}")
                                else:
                                    print(f"[MATCH FAILED] No suitable match found for {label} detection at ({det_center_x:.1f}, {det_center_y:.1f})")
                                    print(f"  -> Will draw as untracked detection with default color")
                            else:
                                if label not in ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']:
                                    print(f"[MATCH DEBUG] Skipping matching for non-vehicle label: {label}")
                                elif len(tracked_vehicles) == 0:
                                    print(f"[MATCH DEBUG] No tracked vehicles available for matching")
                                else:
                                    try:
                                        if len(tracked_vehicles) > 0:
                                            distances = [((det_center_x - (t['bbox'][0] + t['bbox'][2])/2)**2 + (det_center_y - (t['bbox'][1] + t['bbox'][3])/2)**2)**0.5 for t in tracked_vehicles[:3]]
                                            print(f"[DEBUG] No match found for detection at ({det_center_x:.1f},{det_center_y:.1f}) - distances: {distances}")
                                        else:
                                            print(f"[DEBUG] No tracked vehicles available to match detection at ({det_center_x:.1f},{det_center_y:.1f})")
                                    except NameError:
                                        print(f"[DEBUG] No match found for detection (coords unavailable)")
                                        if len(tracked_vehicles) > 0:
                                            print(f"[DEBUG] Had {len(tracked_vehicles)} tracked vehicles available")
                            
                            # Choose box color based on vehicle status 
                            # PRIORITY: 1. Violating (RED) - crossed during red light 2. Moving (ORANGE) 3. Stopped (GREEN)
                            if is_violating_vehicle and vehicle_id is not None:
                                box_color = (0, 0, 255)  # RED for violating vehicles (crossed line during red)
                                label_text = f"{label}:ID{vehicle_id}⚠️"
                                thickness = 4
                                vehicles_violating += 1
                                print(f"[COLOR DEBUG] Drawing RED box for VIOLATING vehicle ID={vehicle_id} (crossed during red)")
                            elif is_moving_vehicle and vehicle_id is not None and not is_violating_vehicle:
                                box_color = (0, 165, 255)  # ORANGE for moving vehicles (not violating)
                                label_text = f"{label}:ID{vehicle_id}"
                                thickness = 3
                                vehicles_moving += 1
                                print(f"[COLOR DEBUG] Drawing ORANGE box for MOVING vehicle ID={vehicle_id} (not violating)")
                            elif label in ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle'] and vehicle_id is not None:
                                box_color = (0, 255, 0)  # Green for stopped vehicles 
                                label_text = f"{label}:ID{vehicle_id}"
                                thickness = 2
                                print(f"[COLOR DEBUG] Drawing GREEN box for STOPPED vehicle ID={vehicle_id}")
                            elif is_traffic_light(label):
                                box_color = (0, 0, 255)  # Red for traffic lights
                                label_text = f"{label}"
                                thickness = 2
                            else:
                                box_color = (0, 255, 0)  # Default green for other objects
                                label_text = f"{label}"
                                thickness = 2
                            
                            # Update statistics
                            if label in ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']:
                                if vehicle_id is not None:
                                    vehicles_with_ids += 1
                                else:
                                    vehicles_without_ids += 1
                            
                            # Draw rectangle and label
                            cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), box_color, thickness)
                            cv2.putText(annotated_frame, label_text, (x1, y1-10), 
                                      cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 2)
                            #     id_text = f"ID: {det['id']}"
                            #     # Calculate text size for background
                            #     (tw, th), baseline = cv2.getTextSize(id_text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
                            #     # Draw filled rectangle for background (top-left of bbox)
                            #     cv2.rectangle(annotated_frame, (x1, y1 - th - 8), (x1 + tw + 4, y1), (0, 0, 0), -1)
                            #     # Draw the ID text in bold yellow
                            #     cv2.putText(annotated_frame, id_text, (x1 + 2, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
                            #     print(f"[DEBUG] Detection ID: {det['id']} BBOX: {bbox} CLASS: {label} CONF: {confidence:.2f}")
                           
                            if class_id == 9 or is_traffic_light(label):
                                try:
                                    light_info = detect_traffic_light_color(annotated_frame, [x1, y1, x2, y2])
                                    if light_info.get("color", "unknown") == "unknown":
                                        light_info = ensure_traffic_light_color(annotated_frame, [x1, y1, x2, y2])
                                    det['traffic_light_color'] = light_info
                                    # Draw enhanced traffic light status
                                    annotated_frame = draw_traffic_light_status(annotated_frame, bbox, light_info)
                                    
                                    # --- Update latest_traffic_light for UI/console ---
                                    self.latest_traffic_light = light_info
                                    
                                    # Add a prominent traffic light status at the top of the frame
                                    color = light_info.get('color', 'unknown')
                                    confidence = light_info.get('confidence', 0.0)
                                    
                                    if color == 'red':
                                        status_color = (0, 0, 255)  # Red
                                        status_text = f"Traffic Light: RED ({confidence:.2f})"
                                        
                                        # Draw a prominent red banner across the top
                                        banner_height = 40
                                        cv2.rectangle(annotated_frame, (0, 0), (annotated_frame.shape[1], banner_height), (0, 0, 150), -1)
                                        
                                        # Add text
                                        font = cv2.FONT_HERSHEY_DUPLEX
                                        font_scale = 0.9
                                        font_thickness = 2
                                        cv2.putText(annotated_frame, status_text, (10, banner_height-12), font, 
                                                  font_scale, (255, 255, 255), font_thickness)
                                except Exception as e:
                                    print(f"[WARN] Could not detect/draw traffic light color: {e}")

                # Print statistics summary
                print(f"[STATS] Vehicles: {vehicles_with_ids} with IDs, {vehicles_without_ids} without IDs")
                print(f"[STATS] Moving: {vehicles_moving}, Violating: {vehicles_violating}")
                
                # Handle multiple traffic lights with consensus approach
                for det in detections:
                    if is_traffic_light(det.get('class_name')):
                        has_traffic_lights = True
                        if 'traffic_light_color' in det:
                            light_info = det['traffic_light_color']
                            traffic_lights.append({'bbox': det['bbox'], 'color': light_info.get('color', 'unknown'), 'confidence': light_info.get('confidence', 0.0)})
                
                # Determine the dominant traffic light color based on confidence
                if traffic_lights:
                    # Filter to just red lights and sort by confidence
                    red_lights = [tl for tl in traffic_lights if tl.get('color') == 'red']
                    if red_lights:
                        # Use the highest confidence red light for display
                        highest_conf_red = max(red_lights, key=lambda x: x.get('confidence', 0))
                        # Update the global traffic light status for consistent UI display
                        self.latest_traffic_light = {
                            'color': 'red',
                            'confidence': highest_conf_red.get('confidence', 0.0)
                        }

                # Emit individual violation signals for each violation
                if violations:
                    for violation in violations:
                        print(f"🚨 Emitting RED LIGHT VIOLATION: Track ID {violation['track_id']}")
                        # Add additional data to the violation
                        violation['frame'] = frame
                        violation['violation_line_y'] = violation_line_y
                        self.violation_detected.emit(violation)
                    print(f"[DEBUG] Emitted {len(violations)} violation signals")
                
                # Add FPS display directly on frame
                # cv2.putText(annotated_frame, f"FPS: {fps_smoothed:.1f}", (10, 30), 
                #            cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

                # # --- Always draw detected traffic light color indicator at top ---
                # color = self.latest_traffic_light.get('color', 'unknown') if isinstance(self.latest_traffic_light, dict) else str(self.latest_traffic_light)
                # confidence = self.latest_traffic_light.get('confidence', 0.0) if isinstance(self.latest_traffic_light, dict) else 0.0
                # indicator_size = 30
                # margin = 10
                # status_colors = {
                #     "red": (0, 0, 255),
                #     "yellow": (0, 255, 255),
                #     "green": (0, 255, 0),
                #     "unknown": (200, 200, 200)
                # }
                # draw_color = status_colors.get(color, (200, 200, 200))
                # # Draw circle indicator
                # cv2.circle(
                #     annotated_frame,
                #     (annotated_frame.shape[1] - margin - indicator_size, margin + indicator_size),
                #     indicator_size,
                #     draw_color,
                #     -1
                # )
                # # Add color text
                # cv2.putText(
                #     annotated_frame,
                #     f"{color.upper()} ({confidence:.2f})",
                #     (annotated_frame.shape[1] - margin - indicator_size - 120, margin + indicator_size + 10),
                #     cv2.FONT_HERSHEY_SIMPLEX,
                #     0.7,
                #     (0, 0, 0),
                #     2
                # )

                # Signal for raw data subscribers (now without violations)
                # Emit with correct number of arguments
                try:
                    self.raw_frame_ready.emit(frame.copy(), detections, fps_smoothed)
                    print(f"✅ raw_frame_ready signal emitted with {len(detections)} detections, fps={fps_smoothed:.1f}")
                except Exception as e:
                    print(f"❌ Error emitting raw_frame_ready: {e}")
                    import traceback
                    traceback.print_exc()
                
                # Emit the NumPy frame signal for direct display - annotated version for visual feedback
                print(f"🔴 Emitting frame_np_ready signal with annotated_frame shape: {annotated_frame.shape}")
                try:
                    # Make sure the frame can be safely transmitted over Qt's signal system
                    # Create a contiguous copy of the array
                    frame_copy = np.ascontiguousarray(annotated_frame)
                    print(f"🔍 Debug - Before emission: frame_copy type={type(frame_copy)}, shape={frame_copy.shape}, is_contiguous={frame_copy.flags['C_CONTIGUOUS']}")
                    self.frame_np_ready.emit(frame_copy)
                    print("✅ frame_np_ready signal emitted successfully")
                except Exception as e:
                    print(f"❌ Error emitting frame: {e}")
                    import traceback
                    traceback.print_exc()
                
                # Emit QPixmap for video detection tab (frame_ready)
                try:
                    from PySide6.QtGui import QImage, QPixmap
                    rgb_frame = cv2.cvtColor(annotated_frame, cv2.COLOR_BGR2RGB)
                    h, w, ch = rgb_frame.shape
                    bytes_per_line = ch * w
                    qimg = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
                    pixmap = QPixmap.fromImage(qimg)
                    metrics = {
                        'FPS': fps_smoothed,
                        'Detection (ms)': detection_time
                    }
                    self.frame_ready.emit(pixmap, detections, metrics)
                    print("✅ frame_ready signal emitted for video detection tab")
                except Exception as e:
                    print(f"❌ Error emitting frame_ready: {e}")
                    import traceback
                    traceback.print_exc()
                
                # Emit stats signal for performance monitoring
                stats = {
                    'fps': fps_smoothed,
                    'detection_fps': fps_smoothed,  # Numeric value for analytics
                    'detection_time': detection_time,
                    'detection_time_ms': detection_time,  # Numeric value for analytics
                    'traffic_light_color': self.latest_traffic_light
                }
                
                # Print detailed stats for debugging
                tl_color = "unknown"
                if isinstance(self.latest_traffic_light, dict):
                    tl_color = self.latest_traffic_light.get('color', 'unknown')
                elif isinstance(self.latest_traffic_light, str):
                    tl_color = self.latest_traffic_light
                
                print(f"🟢 Stats Updated: FPS={fps_smoothed:.2f}, Inference={detection_time:.2f}ms, Traffic Light={tl_color}")
                      
                # Emit stats signal
                self.stats_ready.emit(stats)

                # --- Ensure analytics update every frame ---
                if hasattr(self, 'analytics_controller') and self.analytics_controller is not None:
                    try:
                        self.analytics_controller.process_frame_data(frame, detections, stats)
                        print("[DEBUG] Called analytics_controller.process_frame_data for analytics update")
                    except Exception as e:
                        print(f"[ERROR] Could not update analytics: {e}")
                
                # Control processing rate for file sources
                if isinstance(self.source, str) and self.source_fps > 0:
                    frame_duration = time.time() - process_start
                    if frame_duration < frame_time:
                        time.sleep(frame_time - frame_duration)
            
            cap.release()
        except Exception as e:
            print(f"Video processing error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._running = False
    def _process_frame(self):
        """Process current frame for display with improved error handling"""
        try:
            self.mutex.lock()
            if self.current_frame is None:
                print("⚠️ No frame available to process")
                self.mutex.unlock()
                
                # Check if we're running - if not, this is expected behavior
                if not self._running:
                    return
                
                # If we are running but have no frame, create a blank frame with error message
                h, w = 480, 640  # Default size
                blank_frame = np.zeros((h, w, 3), dtype=np.uint8)
                cv2.putText(blank_frame, "No video input", (w//2-100, h//2), 
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                
                # Emit this blank frame
                try:
                    self.frame_np_ready.emit(blank_frame)
                except Exception as e:
                    print(f"Error emitting blank frame: {e}")
                
                return
            
            # Make a copy of the data we need
            try:
                frame = self.current_frame.copy()
                detections = self.current_detections.copy() if self.current_detections else []
                violations = []  # Violations are disabled
                metrics = self.performance_metrics.copy()
            except Exception as e:
                print(f"Error copying frame data: {e}")
                self.mutex.unlock()
                return
                
            self.mutex.unlock()
        except Exception as e:
            print(f"Critical error in _process_frame initialization: {e}")
            import traceback
            traceback.print_exc()
            try:
                self.mutex.unlock()
            except:
                pass
            return
        
        try:
            # --- Simplified frame processing for display ---
            # The violation logic is now handled in the main _run thread
            # This method just handles basic display overlays
            
            annotated_frame = frame.copy()

            # Add performance overlays and debug markers - COMMENTED OUT for clean video display
            # annotated_frame = draw_performance_overlay(annotated_frame, metrics)
            # cv2.circle(annotated_frame, (20, 20), 10, (255, 255, 0), -1)

            # Convert BGR to RGB before display (for PyQt/PySide)
            frame_rgb = cv2.cvtColor(annotated_frame, cv2.COLOR_BGR2RGB)
            # Display the RGB frame in the UI (replace with your display logic)
            # Example: self.image_label.setPixmap(QPixmap.fromImage(QImage(frame_rgb.data, w, h, QImage.Format_RGB888)))
        except Exception as e:
            print(f"Error in _process_frame: {e}")
            import traceback
            traceback.print_exc()

    def _cleanup_old_vehicle_data(self, current_track_ids):
        """
        Clean up tracking data for vehicles that are no longer being tracked.
        This prevents memory leaks and improves performance.
        
        Args:
            current_track_ids: Set of currently active track IDs
        """
        # Find IDs that are no longer active
        old_ids = set(self.vehicle_history.keys()) - set(current_track_ids)
        
        if old_ids:
            print(f"[CLEANUP] Removing tracking data for {len(old_ids)} old vehicle IDs: {sorted(old_ids)}")
            for old_id in old_ids:
                # Remove from history and status tracking
                if old_id in self.vehicle_history:
                    del self.vehicle_history[old_id]
                if old_id in self.vehicle_statuses:
                    del self.vehicle_statuses[old_id]
            print(f"[CLEANUP] Now tracking {len(self.vehicle_history)} active vehicles")

    # --- Removed unused internal violation line detection methods and RedLightViolationSystem usage ---
    def play(self):
        """Alias for start(), for UI compatibility."""
        self.start()






        from PySide6.QtCore import QObject, Signal, QThread, Qt, QMutex, QWaitCondition, QTimer
from PySide6.QtGui import QImage, QPixmap
import cv2
import time
import numpy as np
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional
import os
import sys
import math
import traceback  # Add this at the top for exception printing

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.annotation_utils import (
    draw_detections, 
    draw_performance_metrics,
    resize_frame_for_display,
    convert_cv_to_qimage,
    convert_cv_to_pixmap,
    pipeline_with_violation_line
)
from utils.enhanced_annotation_utils import (
    enhanced_draw_detections,
    draw_performance_overlay,
    enhanced_cv_to_qimage,
    enhanced_cv_to_pixmap
)
from red_light_violation_pipeline import RedLightViolationPipeline
from utils.traffic_light_utils import detect_traffic_light_color, draw_traffic_light_status, ensure_traffic_light_color
from utils.crosswalk_utils2 import detect_crosswalk_and_violation_line, draw_violation_line, get_violation_line_y
from controllers.bytetrack_tracker import ByteTrackVehicleTracker
TRAFFIC_LIGHT_CLASSES = ["traffic light", "trafficlight", "tl"]
TRAFFIC_LIGHT_NAMES = ['trafficlight', 'traffic light', 'tl', 'signal']

def normalize_class_name(class_name):
    """Normalizes class names from different models/formats to a standard name"""
    if not class_name:
        return ""
    name_lower = class_name.lower()
    # Traffic light variants
    if name_lower in ['traffic light', 'trafficlight', 'traffic_light', 'tl', 'signal']:
        return 'traffic light'
    # Vehicle classes
    if name_lower in ['car', 'auto', 'automobile']:
        return 'car'
    elif name_lower in ['truck']:
        return 'truck'
    elif name_lower in ['bus']:
        return 'bus'
    elif name_lower in ['motorcycle', 'scooter', 'motorbike', 'bike']:
        return 'motorcycle'
    # Person variants
    if name_lower in ['person', 'pedestrian', 'human']:
        return 'person'
    # Add more as needed
    return class_name

def is_traffic_light(class_name):
    """Helper function to check if a class name is a traffic light with normalization"""
    if not class_name:
        return False
        return False
    normalized = normalize_class_name(class_name)
    return normalized == 'traffic light'

class VideoController(QObject):      
    frame_ready = Signal(object, object, dict)  # QPixmap, detections, metrics
    raw_frame_ready = Signal(np.ndarray, list, float)  # frame, detections, fps
    frame_np_ready = Signal(np.ndarray)  # Direct NumPy frame signal for display
    stats_ready = Signal(dict)  # Dictionary with stats (fps, detection_time, traffic_light)
    violation_detected = Signal(dict)  # Signal emitted when a violation is detected
    progress_ready = Signal(int, int, float)  # value, max_value, timestamp (for video progress bar)
    
    def __init__(self, model_manager=None):
        print("[DEBUG] VideoController __init__ called")
        """
        Initialize video controller.
        
        Args:
            model_manager: Model manager instance for detection and violation
        """        
        super().__init__()
        
        self._running = False
        self.source = None
        self.source_type = None
        self.source_fps = 0
        self.performance_metrics = {}
        self.mutex = QMutex()
        
        # Performance tracking
        self.processing_times = deque(maxlen=100)  # Store last 100 processing times
        self.fps_history = deque(maxlen=100)       # Store last 100 FPS values
        self.start_time = time.time()
        self.frame_count = 0
        self.actual_fps = 0.0
        
        self.model_manager = model_manager
        self.inference_model = None
        self.tracker = None
        
        self.current_frame = None
        self.current_detections = []
        
        # Traffic light state tracking
        self.latest_traffic_light = {"color": "unknown", "confidence": 0.0}
        
        # Vehicle tracking settings
        self.vehicle_history = {}  # Dictionary to store vehicle position history
        self.vehicle_statuses = {}  # Track stable movement status
        self.movement_threshold = 1.5  # ADJUSTED: More balanced movement detection (was 0.8)
        self.min_confidence_threshold = 0.3  # FIXED: Lower threshold for better detection (was 0.5)
        
        # Enhanced violation detection settings
        self.position_history_size = 20  # Increased from 10 to track longer history
        self.crossing_check_window = 8   # Check for crossings over the last 8 frames instead of just 2
        self.max_position_jump = 50      # Maximum allowed position jump between frames (detect ID switches)
        
        # Set up violation detection
        try:
            from controllers.red_light_violation_detector import RedLightViolationDetector
            self.violation_detector = RedLightViolationDetector()
            print("✅ Red light violation detector initialized")
        except Exception as e:
            self.violation_detector = None
            print(f"❌ Could not initialize violation detector: {e}")
            
        # Import crosswalk detection
        try:
            self.detect_crosswalk_and_violation_line = detect_crosswalk_and_violation_line
            # self.draw_violation_line = draw_violation_line
            print("✅ Crosswalk detection utilities imported")
        except Exception as e:
            print(f"❌ Could not import crosswalk detection: {e}")
            self.detect_crosswalk_and_violation_line = lambda frame, *args: (None, None, {})
            # self.draw_violation_line = lambda frame, *args, **kwargs: frame
        
        # Configure thread
        self.thread = QThread()
        self.moveToThread(self.thread)
        self.thread.started.connect(self._run)
          # Performance measurement
        self.mutex = QMutex()
        self.condition = QWaitCondition()
        self.performance_metrics = {
            'FPS': 0.0,
            'Detection (ms)': 0.0,
            'Total (ms)': 0.0
        }
        
        # Frame buffer
        self.current_frame = None
        self.current_detections = []
        self.current_violations = []
        
        # Debug counter for monitoring frame processing
        self.debug_counter = 0
        self.violation_frame_counter = 0  # Add counter for violation processing
        
        # Initialize the traffic light color detection pipeline
        self.cv_violation_pipeline = RedLightViolationPipeline(debug=True)
        
        # Initialize vehicle tracker
        self.vehicle_tracker = ByteTrackVehicleTracker()
        
        # Add red light violation system
        # self.red_light_violation_system = RedLightViolationSystem()
        
        # Playback control variables
        self.playback_position = 0  # Current position in the video (in milliseconds)
        self.detection_enabled = True  # Detection enabled/disabled flag
        
    def set_source(self, source):
        """
        Set video source (file path, camera index, or URL)
        
        Args:
            source: Video source - can be a camera index (int), file path (str), 
                   or URL (str). If None, defaults to camera 0.
                   
        Returns:
            bool: True if source was set successfully, False otherwise
        """
        print(f"🎬 VideoController.set_source called with: {source} (type: {type(source)})")
        
        # Store current state
        was_running = self._running
        
        # Stop current processing if running
        if self._running:
            print("⏹️ Stopping current video processing")
            self.stop()
        
        try:
            # Handle source based on type with better error messages
            if source is None:
                print("⚠️ Received None source, defaulting to camera 0")
                self.source = 0
                self.source_type = "camera"
                
            elif isinstance(source, str) and source.strip():
                if os.path.exists(source):
                    # Valid file path
                    self.source = source
                    self.source_type = "file"
                    print(f"📄 Source set to file: {self.source}")
                elif source.lower().startswith(("http://", "https://", "rtsp://", "rtmp://")):
                    # URL stream
                    self.source = source
                    self.source_type = "url"
                    print(f"🌐 Source set to URL stream: {self.source}")
                elif source.isdigit():
                    # String camera index (convert to int)
                    self.source = int(source)
                    self.source_type = "camera"
                    print(f"📹 Source set to camera index: {self.source}")
                else:
                    # Try as device path or special string
                    self.source = source
                    self.source_type = "device"
                    print(f"📱 Source set to device path: {self.source}")
                    
            elif isinstance(source, int):
                # Camera index
                self.source = source
                self.source_type = "camera"
                print(f"📹 Source set to camera index: {self.source}")
                
            else:
                # Unrecognized - default to camera 0 with warning
                print(f"⚠️ Unrecognized source type: {type(source)}, defaulting to camera 0")
                self.source = 0
                self.source_type = "camera"
        except Exception as e:
            print(f"❌ Error setting source: {e}")
            self.source = 0
            self.source_type = "camera"
            return False
        
        # Get properties of the source (fps, dimensions, etc)
        print(f"🔍 Getting properties for source: {self.source}")
        success = self._get_source_properties()
        
        if success:
            print(f"✅ Successfully configured source: {self.source} ({self.source_type})")
            
            # Reset ByteTrack tracker for new source to ensure IDs start from 1
            if hasattr(self, 'vehicle_tracker') and self.vehicle_tracker is not None:
                try:
                    print("🔄 Resetting vehicle tracker for new source")
                    self.vehicle_tracker.reset()
                except Exception as e:
                    print(f"⚠️ Could not reset vehicle tracker: {e}")
            
            # Emit successful source change
            self.stats_ready.emit({
                'source_changed': True,
                'source_type': self.source_type,
                'fps': self.source_fps if hasattr(self, 'source_fps') else 0,
                'dimensions': f"{self.frame_width}x{self.frame_height}" if hasattr(self, 'frame_width') else "unknown"
            })
            
            # Restart if previously running
            if was_running:
                print("▶️ Restarting video processing with new source")
                self.start()
        else:
            print(f"❌ Failed to configure source: {self.source}")
            # Notify UI about the error
            self.stats_ready.emit({
                'source_changed': False,
                'error': f"Invalid video source: {self.source}",
                'source_type': self.source_type,
                'fps': 0,
                'detection_time_ms': "0",
                'traffic_light_color': {"color": "unknown", "confidence": 0.0}
            })
            
            return False
            
        # Return success status
        return success
    
    def _get_source_properties(self):
        """
        Get properties of video source
        
        Returns:
            bool: True if source was successfully opened, False otherwise
        """
        try:
            print(f"🔍 Opening video source for properties check: {self.source}")
            cap = cv2.VideoCapture(self.source)
            
            # Verify capture opened successfully
            if not cap.isOpened():
                print(f"❌ Failed to open video source: {self.source}")
                return False
                
            # Read properties
            self.source_fps = cap.get(cv2.CAP_PROP_FPS)
            if self.source_fps <= 0:
                print("⚠️ Source FPS not available, using default 30 FPS")
                self.source_fps = 30.0  # Default if undetectable
            
            self.frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))                
            self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            
            # Try reading a test frame to confirm source is truly working
            ret, test_frame = cap.read()
            if not ret or test_frame is None:
                print("⚠️ Could not read test frame from source")
                # For camera sources, try one more time with delay
                if self.source_type == "camera":
                    print("🔄 Retrying camera initialization...")
                    time.sleep(1.0)  # Wait a moment for camera to initialize
                    ret, test_frame = cap.read()
                    if not ret or test_frame is None:
                        print("❌ Camera initialization failed after retry")
                        cap.release()
                        return False
                else:
                    print("❌ Could not read frames from video source")
                    cap.release()
                    return False
                
            # Release the capture
            cap.release()
            
            print(f"✅ Video source properties: {self.frame_width}x{self.frame_height}, {self.source_fps} FPS")
            return True
            
        except Exception as e:
            print(f"❌ Error getting source properties: {e}")
            return False
            return False
            
    def start(self):
        """Start video processing"""
        if not self._running:
            self._running = True
            self.start_time = time.time()
            self.frame_count = 0
            self.debug_counter = 0
            print("DEBUG: Starting video processing thread")
            
            # Reset ByteTrack tracker to ensure IDs start from 1
            if hasattr(self, 'vehicle_tracker') and self.vehicle_tracker is not None:
                try:
                    print("🔄 Resetting vehicle tracker for new session")
                    self.vehicle_tracker.reset()
                except Exception as e:
                    print(f"⚠️ Could not reset vehicle tracker: {e}")
            
            # Start the processing thread - add more detailed debugging
            if not self.thread.isRunning():
                print("🚀 Thread not running, starting now...")
                try:
                    self.thread.start()
                    print("✅ Thread started successfully")
                    print(f"🔄 Thread state: running={self.thread.isRunning()}, finished={self.thread.isFinished()}")
                except Exception as e:
                    print(f"❌ Failed to start thread: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print("⚠️ Thread is already running!")
                print(f"🔄 Thread state: running={self.thread.isRunning()}, finished={self.thread.isFinished()}")
    
    def stop(self):
        """Stop video processing"""
        if self._running:
            print("DEBUG: Stopping video processing")
            self._running = False
            # Properly terminate the thread
            self.thread.quit()
            if not self.thread.wait(3000):  # Wait 3 seconds max
                self.thread.terminate()
                print("WARNING: Thread termination forced")
            
            # Clear the current frame
            self.mutex.lock()
            self.current_frame = None
            self.mutex.unlock()
            print("DEBUG: Video processing stopped")
    
    def capture_snapshot(self) -> np.ndarray:
        """Capture current frame"""
        if self.current_frame is not None:
            return self.current_frame.copy()
        return None
        
    def _run(self):
        """Main processing loop (runs in thread)"""
        try:
            print(f"DEBUG: Opening video source: {self.source} (type: {type(self.source)})")
            cap = None
            max_retries = 3
            retry_delay = 1.0
            def try_open_source(src, retries=max_retries, delay=retry_delay):
                for attempt in range(1, retries + 1):
                    print(f"🎥 Opening source (attempt {attempt}/{retries}): {src}")
                    try:
                        capture = cv2.VideoCapture(src)
                        if capture.isOpened():
                            ret, test_frame = capture.read()
                            if ret and test_frame is not None:
                                print(f"✅ Source opened successfully: {src}")
                                if isinstance(src, str) and os.path.exists(src):
                                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                return capture
                            else:
                                print(f"⚠️ Source opened but couldn't read frame: {src}")
                                capture.release()
                        else:
                            print(f"⚠️ Failed to open source: {src}")
                        if attempt < retries:
                            print(f"Retrying in {delay:.1f} seconds...")
                            time.sleep(delay)
                    except Exception as e:
                        print(f"❌ Error opening source {src}: {e}")
                        if attempt < retries:
                            print(f"Retrying in {delay:.1f} seconds...")
                            time.sleep(delay)
                print(f"❌ Failed to open source after {retries} attempts: {src}")
                return None
            if isinstance(self.source, str) and os.path.exists(self.source):
                print(f"📄 Opening video file: {self.source}")
                cap = try_open_source(self.source)
            elif isinstance(self.source, int) or (isinstance(self.source, str) and self.source.isdigit()):
                camera_idx = int(self.source) if isinstance(self.source, str) else self.source
                print(f"📹 Opening camera with index: {camera_idx}")
                cap = try_open_source(camera_idx)
                if cap is None and os.name == 'nt':
                    print("🔄 Trying camera with DirectShow backend...")
                    cap = try_open_source(camera_idx + cv2.CAP_DSHOW)
            else:
                print(f"🌐 Opening source as string: {self.source}")
                cap = try_open_source(str(self.source))
            if cap is None:
                print(f"❌ Failed to open video source after all attempts: {self.source}")
                self.stats_ready.emit({
                    'error': f"Could not open video source: {self.source}",
                    'fps': "0",
                    'detection_time_ms': "0",
                    'traffic_light_color': {"color": "unknown", "confidence": 0.0}
                })
                return
            if not cap or not cap.isOpened():
                print(f"ERROR: Could not open video source {self.source}")
                self.stats_ready.emit({
                    'error': f"Failed to open video source: {self.source}",
                    'fps': "0",
                    'detection_time_ms': "0",
                    'traffic_light_color': {"color": "unknown", "confidence": 0.0}
                })
                return
            frame_time = 1.0 / self.source_fps if self.source_fps > 0 else 0.033
            prev_time = time.time()
            print(f"SUCCESS: Video source opened: {self.source}")
            print(f"Source info - FPS: {self.source_fps}, Size: {self.frame_width}x{self.frame_height}")
            frame_error_count = 0
            max_consecutive_errors = 10
            while self._running and cap.isOpened():
                try:
                    ret, frame = cap.read()
                    print(f"🟡 Frame read attempt: ret={ret}, frame={None if frame is None else frame.shape}")
                    if not ret or frame is None:
                        frame_error_count += 1
                        print(f"⚠️ Frame read error ({frame_error_count}/{max_consecutive_errors})")
                        if frame_error_count >= max_consecutive_errors:
                            print("❌ Too many consecutive frame errors, stopping video thread")
                            break
                        time.sleep(0.1)
                        continue
                    frame_error_count = 0
                except Exception as e:
                    print(f"❌ Critical error reading frame: {e}")
                    frame_error_count += 1
                    if frame_error_count >= max_consecutive_errors:
                        print("❌ Too many errors, stopping video thread")
                        break
                    continue
                process_start = time.time()
                # --- Detection, tracking, annotation, violation logic (single-pass) ---
                detection_start = time.time()
                detections = []
                if self.model_manager:
                    detections = self.model_manager.detect(frame)
                    traffic_light_indices = []
                    for i, det in enumerate(detections):
                        if 'class_name' in det:
                            original_name = det['class_name']
                            normalized_name = normalize_class_name(original_name)
                            if normalized_name == 'traffic light' or original_name == 'traffic light':
                                traffic_light_indices.append(i)
                            if original_name != normalized_name:
                                print(f"📊 Normalized class name: '{original_name}' -> '{normalized_name}'")
                            det['class_name'] = normalized_name
                detection_time = (time.time() - detection_start) * 1000
                violation_start = time.time()
                violations = []
                violation_time = (time.time() - violation_start) * 1000
                if self.model_manager:
                    detections = self.model_manager.update_tracking(detections, frame)
                    if detections and isinstance(detections[0], tuple):
                        detections = [
                            {'id': d[0], 'bbox': d[1], 'confidence': d[2], 'class_id': d[3]}
                            for d in detections
                        ]
                process_time = (time.time() - process_start) * 1000
                self.processing_times.append(process_time)
                now = time.time()
                self.frame_count += 1
                elapsed = now - self.start_time
                if elapsed > 0:
                    self.actual_fps = self.frame_count / elapsed
                fps_smoothed = 1.0 / (now - prev_time) if now > prev_time else 0
                prev_time = now
                self.performance_metrics = {
                    'FPS': f"{fps_smoothed:.1f}",
                    'Detection (ms)': f"{detection_time:.1f}",
                    'Total (ms)': f"{process_time:.1f}"
                }
                self.mutex.lock()
                self.current_frame = frame.copy()
                self.current_detections = detections
                self.mutex.unlock()
                annotated_frame = frame.copy()
                # --- CRITICAL: Always initialize annotated_frame as a copy of frame ---
                # Detection and violation processing
                process_start = time.time()
                
                # Process detections
                detection_start = time.time()
                detections = []
                if self.model_manager:
                    # Always use confidence threshold 0.3
                    detections = self.model_manager.detect(frame)
                    # Normalize class names and assign unique IDs
                    next_vehicle_id = 1
                    used_ids = set()
                    for i, det in enumerate(detections):
                        # Normalize class name
                        if 'class_name' in det:
                            det['class_name'] = normalize_class_name(det['class_name'])
                        # Assign unique ID for vehicles
                        if det.get('class_name') in ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']:
                            if 'id' not in det or det['id'] in used_ids or det['id'] is None:
                                det['id'] = next_vehicle_id
                                det['track_id'] = next_vehicle_id
                                next_vehicle_id += 1
                            else:
                                det['track_id'] = det['id']
                                used_ids.add(det['id'])
                        # Ensure confidence is at least 0.3
                        if 'confidence' not in det or det['confidence'] < 0.3:
                            det['confidence'] = 0.3
                        # Traffic light color detection if unknown
                        if det.get('class_name') == 'traffic light':
                            if 'traffic_light_color' not in det or det['traffic_light_color'] == 'unknown' or (isinstance(det['traffic_light_color'], dict) and det['traffic_light_color'].get('color', 'unknown') == 'unknown'):
                                det['traffic_light_color'] = detect_traffic_light_color(frame, det['bbox'])
                
                detection_time = (time.time() - detection_start) * 1000
                
                # Violation detection is disabled
                violation_start = time.time()
                violations = []
                # if self.model_manager and detections:
                #     violations = self.model_manager.detect_violations(
                #         detections, frame, time.time()
                #     )
                violation_time = (time.time() - violation_start) * 1000
                
                # Update tracking if available
                if self.model_manager:
                    detections = self.model_manager.update_tracking(detections, frame)
                    # If detections are returned as tuples, convert to dicts for downstream code
                    if detections and isinstance(detections[0], tuple):
                        detections = [
                            {'id': d[0], 'bbox': d[1], 'confidence': d[2], 'class_id': d[3]}
                            for d in detections
                        ]
                
                # Calculate timing metrics
                process_time = (time.time() - process_start) * 1000
                self.processing_times.append(process_time)
                
                # Update FPS
                now = time.time()
                self.frame_count += 1
                elapsed = now - self.start_time
                if elapsed > 0:
                    self.actual_fps = self.frame_count / elapsed
                    
                fps_smoothed = 1.0 / (now - prev_time) if now > prev_time else 0
                prev_time = now
                  # Update metrics
                self.performance_metrics = {
                    'FPS': f"{fps_smoothed:.1f}",
                    'Detection (ms)': f"{detection_time:.1f}",
                    'Total (ms)': f"{process_time:.1f}"
                }
                
                # Store current frame data (thread-safe)
                self.mutex.lock()
                self.current_frame = frame.copy()
                self.current_detections = detections
                self.mutex.unlock()
                  # --- DEBUG: Print all detection class_ids and class_names ---
                print("[DEBUG] All detections (class_id, class_name):")
                for det in detections:
                    print(f"  class_id={det.get('class_id')}, class_name={det.get('class_name')}, conf={det.get('confidence')}, bbox={det.get('bbox')}")
                # --- END DEBUG ---

                # --- VIOLATION DETECTION LOGIC (Run BEFORE drawing boxes) ---
                # First get violation information so we can color boxes appropriately
                violating_vehicle_ids = set()  # Track which vehicles are violating
                violations = []
                
                # Initialize traffic light variables
                traffic_lights = []
                has_traffic_lights = False
                
                # Handle multiple traffic lights with consensus approach
                traffic_light_count = 0
                for det in detections:
                    # Accept both class_id and class_name for traffic light
                    is_tl = False
                    if 'class_name' in det:
                        is_tl = is_traffic_light(det.get('class_name'))
                    elif 'class_id' in det:
                        # Map class_id to class_name if possible
                        class_id = det.get('class_id')
                        # You may need to adjust this mapping based on your model
                        if class_id == 0:
                            det['class_name'] = 'traffic light'
                            is_tl = True
                    if is_tl:
                        has_traffic_lights = True
                        traffic_light_count += 1
                        if 'traffic_light_color' in det:
                            light_info = det['traffic_light_color']
                            traffic_lights.append({'bbox': det['bbox'], 'color': light_info.get('color', 'unknown'), 'confidence': light_info.get('confidence', 0.0)})
                print(f"[TRAFFIC LIGHT] Detected {traffic_light_count} traffic light(s), has_traffic_lights={has_traffic_lights}")
                if has_traffic_lights:
                    print(f"[TRAFFIC LIGHT] Traffic light colors: {[tl.get('color', 'unknown') for tl in traffic_lights]}")
                
                # Get traffic light position for crosswalk detection
                traffic_light_position = None
                if has_traffic_lights:
                    for det in detections:
                        if is_traffic_light(det.get('class_name')) and 'bbox' in det:
                            traffic_light_bbox = det['bbox']
                            # Extract center point from bbox for crosswalk utils
                            x1, y1, x2, y2 = traffic_light_bbox
                            traffic_light_position = ((x1 + x2) // 2, (y1 + y2) // 2)
                            break

                # --- DETAILED CROSSWALK DETECTION LOGIC ---
                crosswalk_bbox, violation_line_y, debug_info = None, None, {}
                if has_traffic_lights and traffic_light_position is not None:
                    try:
                        print(f"[CROSSWALK] Traffic light detected at {traffic_light_position}, running crosswalk detection")
                        # Use crosswalk_utils2.py's function to detect crosswalk and violation line
                        annotated_frame, crosswalk_bbox, violation_line_y, debug_info = self.detect_crosswalk_and_violation_line(
                            annotated_frame, traffic_light_position
                        )
                        print(f"[CROSSWALK] Detection result: crosswalk_bbox={{crosswalk_bbox is not None}}, violation_line_y={{violation_line_y}}")
                        # Optionally, draw debug overlays or use debug_info for analytics
                    except Exception as e:
                        print(f"[ERROR] Crosswalk detection failed: {e}")
                        crosswalk_bbox, violation_line_y, debug_info = None, None, {}
                else:
                    print(f"[CROSSWALK] No traffic light detected (has_traffic_lights={{has_traffic_lights}}), skipping crosswalk detection")
                    # NO crosswalk detection without traffic light
                    violation_line_y = None
                
                # Check if crosswalk is detected
                crosswalk_detected = crosswalk_bbox is not None
                stop_line_detected = debug_info.get('stop_line') is not None
                
                # ALWAYS process vehicle tracking (moved outside violation logic)
                tracked_vehicles = []
                if hasattr(self, 'vehicle_tracker') and self.vehicle_tracker is not None:
                    # Filter vehicle detections
                    vehicle_classes = ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']
                    vehicle_dets = []
                    h, w = frame.shape[:2]
                    print(f"[TRACK DEBUG] All detections:")
                    for det in detections:
                        print(f"  Det: class={det.get('class_name')}, conf={det.get('confidence')}, bbox={det.get('bbox')}")
                    for det in detections:
                        if (det.get('class_name') in vehicle_classes and 
                            'bbox' in det and 
                            det.get('confidence', 0) > self.min_confidence_threshold):
                            # Check bbox dimensions
                            bbox = det['bbox']
                            x1, y1, x2, y2 = bbox
                            box_w, box_h = x2-x1, y2-y1
                            box_area = box_w * box_h
                            area_ratio = box_area / (w * h)
                            print(f"[TRACK DEBUG] Vehicle {det.get('class_name')} conf={det.get('confidence'):.2f}, area_ratio={area_ratio:.4f}")
                            if 0.0005 <= area_ratio <= 0.25:  # Loosened lower bound
                                vehicle_dets.append(det)
                                print(f"[TRACK DEBUG] Added vehicle: {det.get('class_name')} conf={det.get('confidence'):.2f}")
                            else:
                                print(f"[TRACK DEBUG] Rejected vehicle: area_ratio={area_ratio:.4f} not in range [0.0005, 0.25]")
                    print(f"[TRACK DEBUG] Filtered to {len(vehicle_dets)} vehicle detections")
                    # Update tracker
                    if len(vehicle_dets) > 0:
                        print(f"[TRACK DEBUG] Updating tracker with {len(vehicle_dets)} vehicles...")
                        tracks = self.vehicle_tracker.update(vehicle_dets, frame)
                        print(f"[TRACK DEBUG] Tracker returned {len(tracks)} tracks")
                    else:
                        print(f"[TRACK DEBUG] No vehicles to track, skipping tracker update")
                        tracks = []
                    # Process each tracked vehicle
                    tracked_vehicles = []
                    track_ids_seen = []
                    for track in tracks:
                        # Only use dict access for tracker output
                        if not isinstance(track, dict) or 'bbox' not in track or track['bbox'] is None:
                            print(f"Warning: Track has no bbox, skipping: {track}")
                            continue
                        print(f"[TRACK DEBUG] Tracker output: {track}")
                        track_id = track.get('id')
                        bbox = track.get('bbox')
                        if bbox is None:
                            print(f"Warning: Track has no bbox, skipping: {track}")
                            continue
                        x1, y1, x2, y2 = map(float, bbox)
                        # Use y2 (bottom of bbox) for robust line crossing
                        bottom_y = y2
                        center_y = (y1 + y2) / 2
                        
                        # Check for duplicate IDs
                        if track_id in track_ids_seen:
                            print(f"[TRACK ERROR] Duplicate ID detected: {track_id}")
                        track_ids_seen.append(track_id)
                        
                        print(f"[TRACK DEBUG] Processing track ID={track_id} bbox={bbox}")
                        
                        # Initialize or update vehicle history
                        if track_id not in self.vehicle_history:
                            from collections import deque
                            self.vehicle_history[track_id] = deque(maxlen=self.position_history_size)
                        
                        # Initialize vehicle status if not exists
                        if track_id not in self.vehicle_statuses:
                            self.vehicle_statuses[track_id] = {
                                'recent_movement': [],
                                'violation_history': [],
                                'crossed_during_red': False,
                                'last_position': None,  # Track last position for jump detection
                                'suspicious_jumps': 0   # Count suspicious position jumps
                            }
                        
                        # Detect suspicious position jumps (potential ID switches)
                        if self.vehicle_statuses[track_id]['last_position'] is not None:
                            last_y = self.vehicle_statuses[track_id]['last_position']
                            position_jump = abs(center_y - last_y)
                            
                            if position_jump > self.max_position_jump:
                                self.vehicle_statuses[track_id]['suspicious_jumps'] += 1
                                print(f"[TRACK WARNING] Vehicle ID={track_id} suspicious position jump: {last_y:.1f} -> {center_y:.1f} (jump={position_jump:.1f})")
                                
                                # If too many suspicious jumps, reset violation status to be safe
                                if self.vehicle_statuses[track_id]['suspicious_jumps'] > 2:
                                    print(f"[TRACK RESET] Vehicle ID={track_id} has too many suspicious jumps, resetting violation status")
                                    self.vehicle_statuses[track_id]['crossed_during_red'] = False
                                    self.vehicle_statuses[track_id]['suspicious_jumps'] = 0
                        
                        # Update position history and last position
                        self.vehicle_history[track_id].append(bottom_y)  # Use bottom_y instead of center_y
                        self.vehicle_statuses[track_id]['last_position'] = bottom_y
                        
                        # BALANCED movement detection - detect clear movement while avoiding false positives
                        is_moving = False
                        movement_detected = False
                        
                        if len(self.vehicle_history[track_id]) >= 3:  # Require at least 3 frames for movement detection
                            recent_positions = list(self.vehicle_history[track_id])
                            
                            # Check movement over 3 frames for quick response
                            if len(recent_positions) >= 3:
                                movement_3frames = abs(recent_positions[-1] - recent_positions[-3])
                                if movement_3frames > self.movement_threshold:  # More responsive threshold
                                    movement_detected = True
                                    print(f"[MOVEMENT] Vehicle ID={track_id} MOVING: 3-frame movement = {movement_3frames:.1f}")
                        
                            # Confirm with longer movement for stability (if available)
                            if len(recent_positions) >= 5:
                                movement_5frames = abs(recent_positions[-1] - recent_positions[-5])
                                if movement_5frames > self.movement_threshold * 1.5:  # Moderate threshold for 5 frames
                                    movement_detected = True
                                    print(f"[MOVEMENT] Vehicle ID={track_id} MOVING: 5-frame movement = {movement_5frames:.1f}")
                        
                        # Store historical movement for smoothing - require consistent movement
                        self.vehicle_statuses[track_id]['recent_movement'].append(movement_detected)
                        if len(self.vehicle_statuses[track_id]['recent_movement']) > 4:  # Shorter history for quicker response
                            self.vehicle_statuses[track_id]['recent_movement'].pop(0)
                        
                        # BALANCED: Require majority of recent frames to show movement (2 out of 4)
                        recent_movement_count = sum(self.vehicle_statuses[track_id]['recent_movement'])
                        total_recent_frames = len(self.vehicle_statuses[track_id]['recent_movement'])
                        if total_recent_frames >= 2 and recent_movement_count >= (total_recent_frames * 0.5):  # 50% of frames must show movement
                            is_moving = True
                        
                        print(f"[TRACK DEBUG] Vehicle ID={track_id} is_moving={is_moving} (threshold={self.movement_threshold})")
                        
                        # Initialize as not violating
                        is_violation = False
                        
                        tracked_vehicles.append({
                            'id': track_id,
                            'bbox': bbox,
                            'center_y': center_y,
                            'bottom_y': bottom_y,
                            'is_moving': is_moving,
                            'is_violation': is_violation
                        })
                # Process violations - CHECK VEHICLES THAT CROSS THE LINE OVER A WINDOW OF FRAMES
                # IMPORTANT: Only process violations if traffic light is detected AND violation line exists
                if has_traffic_lights and violation_line_y is not None and tracked_vehicles:
                    print(f"[VIOLATION DEBUG] Traffic light present, checking {len(tracked_vehicles)} vehicles against violation line at y={violation_line_y}")
                    # Check each tracked vehicle for violations
                    for tracked in tracked_vehicles:
                        track_id = tracked['id']
                        bottom_y = tracked['bottom_y']
                        is_moving = tracked['is_moving']
                        # Get position history for this vehicle
                        position_history = list(self.vehicle_history[track_id])
                        # Enhanced crossing detection: check over a window of frames
                        line_crossed_in_window = False
                        crossing_details = None
                        if len(position_history) >= 2:
                            window_size = min(self.crossing_check_window, len(position_history))
                            for i in range(1, window_size):
                                prev_y = position_history[-(i+1)]  # Earlier position (bottom_y)
                                curr_y = position_history[-i]     # Later position (bottom_y)
                                if prev_y < violation_line_y and curr_y >= violation_line_y:
                                    line_crossed_in_window = True
                                    crossing_details = {
                                        'frames_ago': i,
                                        'prev_y': prev_y,
                                        'curr_y': curr_y,
                                        'window_checked': window_size
                                    }
                                    print(f"[VIOLATION DEBUG] Vehicle ID={track_id} crossed line {i} frames ago: {prev_y:.1f} -> {curr_y:.1f}")
                                    break
                        is_red_light = self.latest_traffic_light and self.latest_traffic_light.get('color') == 'red'
                        actively_crossing = (line_crossed_in_window and is_moving and is_red_light)
                        if 'crossed_during_red' not in self.vehicle_statuses[track_id]:
                            self.vehicle_statuses[track_id]['crossed_during_red'] = False
                        if actively_crossing:
                            suspicious_jumps = self.vehicle_statuses[track_id].get('suspicious_jumps', 0)
                            if suspicious_jumps <= 1:
                                self.vehicle_statuses[track_id]['crossed_during_red'] = True
                                print(f"[VIOLATION ALERT] Vehicle ID={track_id} CROSSED line during red light!")
                                print(f"  -> Crossing details: {crossing_details}")
                            else:
                                print(f"[VIOLATION IGNORED] Vehicle ID={track_id} crossing ignored due to {suspicious_jumps} suspicious jumps")
                        if not is_red_light:
                            if self.vehicle_statuses[track_id]['crossed_during_red']:
                                print(f"[VIOLATION RESET] Vehicle ID={track_id} violation status reset (light turned green)")
                            self.vehicle_statuses[track_id]['crossed_during_red'] = False
                        is_violation = (self.vehicle_statuses[track_id]['crossed_during_red'] and is_red_light)
                        self.vehicle_statuses[track_id]['violation_history'].append(actively_crossing)
                        if len(self.vehicle_statuses[track_id]['violation_history']) > 5:
                            self.vehicle_statuses[track_id]['violation_history'].pop(0)
                        tracked['is_violation'] = is_violation
                        if actively_crossing and self.vehicle_statuses[track_id].get('suspicious_jumps', 0) <= 1:
                            violating_vehicle_ids.add(track_id)
                            timestamp = datetime.now()
                            violations.append({
                                'track_id': track_id,
                                'id': track_id,
                                'bbox': [int(tracked['bbox'][0]), int(tracked['bbox'][1]), int(tracked['bbox'][2]), int(tracked['bbox'][3])],
                                'violation': 'line_crossing',
                                'violation_type': 'line_crossing',
                                'timestamp': timestamp,
                                'line_position': violation_line_y,
                                'movement': crossing_details if crossing_details else {'prev_y': bottom_y, 'current_y': bottom_y},
                                'crossing_window': self.crossing_check_window,
                                'position_history': list(position_history[-10:])
                            })
                            print(f"[DEBUG] 🚨 VIOLATION DETECTED: Vehicle ID={track_id} CROSSED VIOLATION LINE")
                            print(f"    Enhanced detection: {crossing_details}")
                            print(f"    Position history: {[f'{p:.1f}' for p in position_history[-10:]]}")
                            print(f"    Detection window: {self.crossing_check_window} frames")
                            print(f"    while RED LIGHT & MOVING")
                # --- DRAWING/ANNOTATION LOGIC (add overlays before emitting frame) ---
                # 1. Draw vehicle bounding boxes and IDs
                for tracked in tracked_vehicles:
                    bbox = tracked['bbox']
                    track_id = tracked['id']
                    is_violation = tracked.get('is_violation', False)
                    color = (0, 0, 255) if is_violation else (0, 255, 0)
                    cv2.rectangle(annotated_frame, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), color, 2)
                    cv2.putText(annotated_frame, f'ID:{track_id}', (int(bbox[0]), int(bbox[1])-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

                # 2. Draw traffic light color box
                if has_traffic_lights and len(traffic_lights) > 0:
                    for tl in traffic_lights:
                        bbox = tl.get('bbox')
                        color_name = tl.get('color', 'unknown')
                        color_map = {'red': (0,0,255), 'yellow': (0,255,255), 'green': (0,255,0)}
                        box_color = color_map.get(color_name, (255,255,255))
                        if bbox is not None:
                            cv2.rectangle(annotated_frame, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), box_color, 2)
                            cv2.putText(annotated_frame, color_name, (int(bbox[0]), int(bbox[1])-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 2)

                # 3. Draw violation line
                if violation_line_y is not None:
                    cv2.line(annotated_frame, (0, int(violation_line_y)), (annotated_frame.shape[1], int(violation_line_y)), (0,0,255), 3)

                # --- Frame emission logic (robust, single-pass) ---
                # Emit raw_frame_ready (original frame, detections, fps)
                self.raw_frame_ready.emit(frame.copy(), list(detections), self.actual_fps)
                # Emit frame_np_ready (annotated frame for display)
                self.frame_np_ready.emit(annotated_frame)
                # Emit frame_ready (QPixmap, detections, metrics)
                try:
                    pixmap = convert_cv_to_pixmap(annotated_frame)
                except Exception as e:
                    print(f"[ERROR] convert_cv_to_pixmap failed: {e}")
                    pixmap = None
                self.frame_ready.emit(pixmap, list(detections), dict(self.performance_metrics))
                # Emit stats_ready (metrics)
                stats = dict(self.performance_metrics)
                if hasattr(self, 'latest_traffic_light'):
                    stats['traffic_light_color'] = self.latest_traffic_light
                self.stats_ready.emit(stats)
        except Exception as e:
            print(f"Video processing error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._running = False

    def _cleanup_old_vehicle_data(self, current_track_ids):
        """
        Clean up tracking data for vehicles that are no longer being tracked.
        This prevents memory leaks and improves performance.
        
        Args:
            current_track_ids: Set of currently active track IDs
        """
        # Find IDs that are no longer active
        old_ids = set(self.vehicle_history.keys()) - set(current_track_ids)
        
        if old_ids:
            print(f"[CLEANUP] Removing tracking data for {len(old_ids)} old vehicle IDs: {sorted(old_ids)}")
            
            for old_id in old_ids:
                # Remove from history and status tracking
                if old_id in self.vehicle_history:
                    del self.vehicle_history[old_id]
                if old_id in self.vehicle_statuses:
                    del self.vehicle_statuses[old_id]
            
            print(f"[CLEANUP] Now tracking {len(self.vehicle_history)} active vehicles")

    def play(self):
        """Start or resume video playback (for file sources)"""
        print("[VideoController] play() called")
        self.start()

    def pause(self):
        """Pause video playback (for file sources)"""
        print("[VideoController] pause() called")
        # No render_timer

    def seek(self, value):
        """Seek to a specific frame (for file sources)"""
        print(f"[VideoController] seek() called with value: {value}")
        if self.source_type == "file" and hasattr(self, 'cap') and self.cap is not None:
            try:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, value)
                print(f"[VideoController] Seeked to frame {value}")
            except Exception as e:
                print(f"[VideoController] Seek failed: {e}")
        else:
            print("[VideoController] Seek not supported for this source type.")

    def set_detection_enabled(self, enabled):
        """Enable or disable detection during playback"""
        print(f"[VideoController] set_detection_enabled({enabled}) called")
        self.detection_enabled = enabled

    # In your _process_frame or detection logic, wrap detection with:
    # if self.detection_enabled:
    #     ... run detection ...
    # else:
    #     ... skip detection ...

    from PySide6.QtCore import QObject, Signal, QThread, Qt, QMutex, QWaitCondition, QTimer
from PySide6.QtGui import QImage, QPixmap
import cv2
import time
import numpy as np
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional
import os
import sys
import math

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import utilities
from utils.annotation_utils import (
    draw_detections, 
    draw_performance_metrics,
    resize_frame_for_display,
    convert_cv_to_qimage,
    convert_cv_to_pixmap,
    pipeline_with_violation_line
)

# Import enhanced annotation utilities
from utils.enhanced_annotation_utils import (
    enhanced_draw_detections,
    draw_performance_overlay,
    enhanced_cv_to_qimage,
    enhanced_cv_to_pixmap
)

# Import traffic light color detection utilities
from red_light_violation_pipeline import RedLightViolationPipeline
from utils.traffic_light_utils import detect_traffic_light_color, draw_traffic_light_status, ensure_traffic_light_color
from utils.crosswalk_utils2 import detect_crosswalk_and_violation_line, draw_violation_line, get_violation_line_y
from controllers.bytetrack_tracker import ByteTrackVehicleTracker
TRAFFIC_LIGHT_CLASSES = ["traffic light", "trafficlight", "tl"]
TRAFFIC_LIGHT_NAMES = ['trafficlight', 'traffic light', 'tl', 'signal']

def normalize_class_name(class_name):
    """Normalizes class names from different models/formats to a standard name"""
    if not class_name:
        return ""
    
    name_lower = class_name.lower()
    
    # Traffic light variants
    if name_lower in ['traffic light', 'trafficlight', 'traffic_light', 'tl', 'signal']:
        return 'traffic light'
    
    # Keep specific vehicle classes (car, truck, bus) separate
    # Just normalize naming variations within each class
    if name_lower in ['car', 'auto', 'automobile']:
        return 'car'
    elif name_lower in ['truck']:
        return 'truck'
    elif name_lower in ['bus']:
        return 'bus'
    elif name_lower in ['motorcycle', 'scooter', 'motorbike', 'bike']:
        return 'motorcycle'
    
    # Person variants
    if name_lower in ['person', 'pedestrian', 'human']:
        return 'person'
    
    # Other common classes can be added here
    
    return class_name

def is_traffic_light(class_name):
    """Helper function to check if a class name is a traffic light with normalization"""
    if not class_name:
        return False
    normalized = normalize_class_name(class_name)
    return normalized == 'traffic light'

class VideoController(QObject):      
    frame_ready = Signal(object, object, dict)  # QPixmap, detections, metrics
    raw_frame_ready = Signal(np.ndarray, list, float)  # frame, detections, fps
    frame_np_ready = Signal(np.ndarray)  # Direct NumPy frame signal for display
    stats_ready = Signal(dict)  # Dictionary with stats (fps, detection_time, traffic_light)
    violation_detected = Signal(dict)  # Signal emitted when a violation is detected
    progress_ready = Signal(int, int, float)  # value, max_value, timestamp
    auto_select_model_device = Signal()
    device_info_ready = Signal(dict)  # Signal emitted when OpenVINO device info is ready
    
    def __init__(self, model_manager=None):
        """
        Initialize video controller.
        
        Args:
            model_manager: Model manager instance for detection and violation
        """        
        super().__init__()
        print("Loaded advanced VideoController from video_controller_new.py")  # DEBUG: Confirm correct controller
        
        self._running = False
        self.source = None
        self.source_type = None
        self.source_fps = 0
        self.performance_metrics = {}
        self.mutex = QMutex()
        
        # Performance tracking
        self.processing_times = deque(maxlen=100)  # Store last 100 processing times
        self.fps_history = deque(maxlen=100)       # Store last 100 FPS values
        self.start_time = time.time()
        self.frame_count = 0
        self.actual_fps = 0.0
        
        self.model_manager = model_manager
        self.inference_model = None
        self.tracker = None
        
        self.current_frame = None
        self.current_detections = []
        
        # Traffic light state tracking
        self.latest_traffic_light = {"color": "unknown", "confidence": 0.0}
        
        # Vehicle tracking settings
        self.vehicle_history = {}  # Dictionary to store vehicle position history
        self.vehicle_statuses = {}  # Track stable movement status
        self.movement_threshold = 1.5  # ADJUSTED: More balanced movement detection (was 0.8)
        self.min_confidence_threshold = 0.3  # FIXED: Lower threshold for better detection (was 0.5)
        
        # Enhanced violation detection settings
        self.position_history_size = 20  # Increased from 10 to track longer history
        self.crossing_check_window = 8   # Check for crossings over the last 8 frames instead of just 2
        self.max_position_jump = 50      # Maximum allowed position jump between frames (detect ID switches)
        
        # Set up violation detection
        try:
            from controllers.red_light_violation_detector import RedLightViolationDetector
            self.violation_detector = RedLightViolationDetector()
            print("✅ Red light violation detector initialized")
        except Exception as e:
            self.violation_detector = None
            print(f"❌ Could not initialize violation detector: {e}")
            
        # Import crosswalk detection
        try:
            self.detect_crosswalk_and_violation_line = detect_crosswalk_and_violation_line
            # self.draw_violation_line = draw_violation_line
            print("✅ Crosswalk detection utilities imported")
        except Exception as e:
            print(f"❌ Could not import crosswalk detection: {e}")
            self.detect_crosswalk_and_violation_line = lambda frame, *args: (None, None, {})
            # self.draw_violation_line = lambda frame, *args, **kwargs: frame
        
        # Configure thread
        self.thread = QThread()
        self.moveToThread(self.thread)
        self.thread.started.connect(self._run)
          # Performance measurement
        self.mutex = QMutex()
        self.condition = QWaitCondition()
        self.performance_metrics = {
            'FPS': 0.0,
            'Detection (ms)': 0.0,
            'Total (ms)': 0.0
        }
        
        # Setup render timer with more aggressive settings for UI updates
        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self._process_frame)
        
        # Frame buffer
        self.current_frame = None
        self.current_detections = []
        self.current_violations = []
        
        # Debug counter for monitoring frame processing
        self.debug_counter = 0
        self.violation_frame_counter = 0  # Add counter for violation processing
        
        # Initialize the traffic light color detection pipeline
        self.cv_violation_pipeline = RedLightViolationPipeline(debug=True)
        
        # Initialize vehicle tracker
        self.vehicle_tracker = ByteTrackVehicleTracker()
        
        # Add red light violation system
        # self.red_light_violation_system = RedLightViolationSystem()
        
        # Query OpenVINO devices at startup and emit info
        self.query_openvino_devices()

    def query_openvino_devices(self):
        """
        Query available OpenVINO devices and their properties, emit device_info_ready signal.
        """
        try:
            from openvino.runtime import Core
            core = Core()
            devices = core.available_devices
            device_info = {}
            for device in devices:
                try:
                    properties = core.get_property(device, {})
                except Exception:
                    properties = {}
                device_info[device] = properties
            print(f"[OpenVINO] Available devices: {device_info}")
            self.device_info_ready.emit(device_info)
        except Exception as e:
            print(f"[OpenVINO] Could not query devices: {e}")
            self.device_info_ready.emit({'error': str(e)})
            
    def set_source(self, source):
        """
        Set video source (file path, camera index, or URL)
        
        Args:
            source: Video source - can be a camera index (int), file path (str), 
                   or URL (str). If None, defaults to camera 0.
                   
        Returns:
            bool: True if source was set successfully, False otherwise
        """
        print(f"🎬 VideoController.set_source called with: {source} (type: {type(source)})")
        
        # Store current state
        was_running = self._running
        
        # Stop current processing if running
        if self._running:
            print("⏹️ Stopping current video processing")
            self.stop()
        
        try:
            # Handle source based on type with better error messages
            if source is None:
                print("⚠️ Received None source, defaulting to camera 0")
                self.source = 0
                self.source_type = "camera"
                
            elif isinstance(source, str) and source.strip():
                if os.path.exists(source):
                    # Valid file path
                    self.source = source
                    self.source_type = "file"
                    print(f"📄 Source set to file: {self.source}")
                elif source.lower().startswith(("http://", "https://", "rtsp://", "rtmp://")):
                    # URL stream
                    self.source = source
                    self.source_type = "url"
                    print(f"🌐 Source set to URL stream: {self.source}")
                elif source.isdigit():
                    # String camera index (convert to int)
                    self.source = int(source)
                    self.source_type = "camera"
                    print(f"📹 Source set to camera index: {self.source}")
                else:
                    # Try as device path or special string
                    self.source = source
                    self.source_type = "device"
                    print(f"📱 Source set to device path: {self.source}")
                    
            elif isinstance(source, int):
                # Camera index
                self.source = source
                self.source_type = "camera"
                print(f"📹 Source set to camera index: {self.source}")
                
            else:
                # Unrecognized - default to camera 0 with warning
                print(f"⚠️ Unrecognized source type: {type(source)}, defaulting to camera 0")
                self.source = 0
                self.source_type = "camera"
        except Exception as e:
            print(f"❌ Error setting source: {e}")
            self.source = 0
            self.source_type = "camera"
            return False
        
        # Get properties of the source (fps, dimensions, etc)
        print(f"🔍 Getting properties for source: {self.source}")
        success = self._get_source_properties()
        
        if success:
            print(f"✅ Successfully configured source: {self.source} ({self.source_type})")
            
            # Reset ByteTrack tracker for new source to ensure IDs start from 1
            if hasattr(self, 'vehicle_tracker') and self.vehicle_tracker is not None:
                try:
                    print("🔄 Resetting vehicle tracker for new source")
                    self.vehicle_tracker.reset()
                except Exception as e:
                    print(f"⚠️ Could not reset vehicle tracker: {e}")
            
            # Emit successful source change
            self.stats_ready.emit({
                'source_changed': True,
                'source_type': self.source_type,
                'fps': self.source_fps if hasattr(self, 'source_fps') else 0,
                'dimensions': f"{self.frame_width}x{self.frame_height}" if hasattr(self, 'frame_width') else "unknown"
            })
            
            # Restart if previously running
            if was_running:
                print("▶️ Restarting video processing with new source")
                self.start()
        else:
            print(f"❌ Failed to configure source: {self.source}")
            # Notify UI about the error
            self.stats_ready.emit({
                'source_changed': False,
                'error': f"Invalid video source: {self.source}",
                'source_type': self.source_type,
                'fps': 0,
                'detection_time_ms': "0",
                'traffic_light_color': {"color": "unknown", "confidence": 0.0}
            })
            
            return False
            
        # Return success status
        return success
    
    def _get_source_properties(self):
        """
        Get properties of video source
        
        Returns:
            bool: True if source was successfully opened, False otherwise
        """
        try:
            print(f"🔍 Opening video source for properties check: {self.source}")
            cap = cv2.VideoCapture(self.source)
            
            # Verify capture opened successfully
            if not cap.isOpened():
                print(f"❌ Failed to open video source: {self.source}")
                return False
                
            # Read properties
            self.source_fps = cap.get(cv2.CAP_PROP_FPS)
            if self.source_fps <= 0:
                print("⚠️ Source FPS not available, using default 30 FPS")
                self.source_fps = 30.0  # Default if undetectable
            
            self.frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))                
            self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            
            # Try reading a test frame to confirm source is truly working
            ret, test_frame = cap.read()
            if not ret or test_frame is None:
                print("⚠️ Could not read test frame from source")
                # For camera sources, try one more time with delay
                if self.source_type == "camera":
                    print("🔄 Retrying camera initialization...")
                    time.sleep(1.0)  # Wait a moment for camera to initialize
                    ret, test_frame = cap.read()
                    if not ret or test_frame is None:
                        print("❌ Camera initialization failed after retry")
                        cap.release()
                        return False
                else:
                    print("❌ Could not read frames from video source")
                    cap.release()
                    return False
                
            # Release the capture
            cap.release()
            
            print(f"✅ Video source properties: {self.frame_width}x{self.frame_height}, {self.source_fps} FPS")
            return True
            
        except Exception as e:
            print(f"❌ Error getting source properties: {e}")
            return False
            return False
            
    def start(self):
        """Start video processing"""
        if not self._running:
            self._running = True
            self.start_time = time.time()
            self.frame_count = 0
            self.debug_counter = 0
            print("DEBUG: Starting video processing thread")
            
            # Reset ByteTrack tracker to ensure IDs start from 1
            if hasattr(self, 'vehicle_tracker') and self.vehicle_tracker is not None:
                try:
                    print("🔄 Resetting vehicle tracker for new session")
                    self.vehicle_tracker.reset()
                except Exception as e:
                    print(f"⚠️ Could not reset vehicle tracker: {e}")
            
            # Start the processing thread - add more detailed debugging
            if not self.thread.isRunning():
                print("🚀 Thread not running, starting now...")
                try:
                    self.thread.start()
                    print("✅ Thread started successfully")
                    print(f"🔄 Thread state: running={self.thread.isRunning()}, finished={self.thread.isFinished()}")
                except Exception as e:
                    print(f"❌ Failed to start thread: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print("⚠️ Thread is already running!")
                print(f"🔄 Thread state: running={self.thread.isRunning()}, finished={self.thread.isFinished()}")
            
            # Start the render timer with a very aggressive interval (10ms = 100fps)
            # This ensures we can process frames as quickly as possible
            print("⏱️ Starting render timer...")
            self.render_timer.start(10)
            print("✅ Render timer started at 100Hz")
    
    def stop(self):
        """Stop video processing"""
        if self._running:
            print("DEBUG: Stopping video processing")
            self._running = False
            self.render_timer.stop()
            # Properly terminate the thread
            if self.thread.isRunning():
                self.thread.quit()
                if not self.thread.wait(3000):  # Wait 3 seconds max
                    self.thread.terminate()
                    print("WARNING: Thread termination forced")
            # Clear the current frame
            self.mutex.lock()
            self.current_frame = None
            self.mutex.unlock()
            print("DEBUG: Video processing stopped")

    def play(self):
        """Start or resume video processing."""
        if not self._running:
            self._running = True
            if not self.thread.isRunning():
                self.thread.start()
            if hasattr(self, 'render_timer') and not self.render_timer.isActive():
                self.render_timer.start(30)

    def pause(self):
        """Pause video processing (stop timer, keep thread alive)."""
        if hasattr(self, 'render_timer') and self.render_timer.isActive():
            self.render_timer.stop()
        self._running = False

    def __del__(self):
        print("[VideoController] __del__ called. Cleaning up thread and timer.")
        self.stop()
        if self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(1000)
        self.render_timer.stop()
    
    def capture_snapshot(self) -> np.ndarray:
        """Capture current frame"""
        if self.current_frame is not None:
            return self.current_frame.copy()
        return None
        
    def _run(self):
        """Main processing loop (runs in thread)"""
        try:
            # Print the source we're trying to open
            print(f"DEBUG: Opening video source: {self.source} (type: {type(self.source)})")
            
            cap = None  # Initialize capture variable
            
            # Try to open source with more robust error handling
            max_retries = 3
            retry_delay = 1.0  # seconds
            
            # Function to attempt opening the source with multiple retries
            def try_open_source(src, retries=max_retries, delay=retry_delay):
                for attempt in range(1, retries + 1):
                    print(f"🎥 Opening source (attempt {attempt}/{retries}): {src}")
                    try:
                        capture = cv2.VideoCapture(src)
                        if capture.isOpened():
                            # Try to read a test frame to confirm it's working
                            ret, test_frame = capture.read()
                            if ret and test_frame is not None:
                                print(f"✅ Source opened successfully: {src}")
                                # Reset capture position for file sources
                                if isinstance(src, str) and os.path.exists(src):
                                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                return capture
                            else:
                                print(f"⚠️ Source opened but couldn't read frame: {src}")
                                capture.release()
                        else:
                            print(f"⚠️ Failed to open source: {src}")
                            
                        # Retry after delay
                        if attempt < retries:
                            print(f"Retrying in {delay:.1f} seconds...")
                            time.sleep(delay)
                    except Exception as e:
                        print(f"❌ Error opening source {src}: {e}")
                        if attempt < retries:
                            print(f"Retrying in {delay:.1f} seconds...")
                            time.sleep(delay)
                
                print(f"❌ Failed to open source after {retries} attempts: {src}")
                return None
            
            # Handle different source types
            if isinstance(self.source, str) and os.path.exists(self.source):
                # It's a valid file path
                print(f"📄 Opening video file: {self.source}")
                cap = try_open_source(self.source)
                
            elif isinstance(self.source, int) or (isinstance(self.source, str) and self.source.isdigit()):
                # It's a camera index
                camera_idx = int(self.source) if isinstance(self.source, str) else self.source
                print(f"📹 Opening camera with index: {camera_idx}")
                
                # For cameras, try with different backend options if it fails
                cap = try_open_source(camera_idx)
                
                # If failed, try with DirectShow backend on Windows
                if cap is None and os.name == 'nt':
                    print("🔄 Trying camera with DirectShow backend...")
                    cap = try_open_source(camera_idx + cv2.CAP_DSHOW)
                    
            else:
                # Try as a string source (URL or device path)
                print(f"🌐 Opening source as string: {self.source}")
                cap = try_open_source(str(self.source))
                
            # Check if we successfully opened the source
            if cap is None:
                print(f"❌ Failed to open video source after all attempts: {self.source}")
                # Notify UI about the error
                self.stats_ready.emit({
                    'error': f"Could not open video source: {self.source}",
                    'fps': "0",
                    'detection_time_ms': "0",
                    'traffic_light_color': {"color": "unknown", "confidence": 0.0}
                })
                return
                    
            # Check again to ensure capture is valid
            if not cap or not cap.isOpened():
                print(f"ERROR: Could not open video source {self.source}")
                # Emit a signal to notify UI about the error
                self.stats_ready.emit({
                    'error': f"Failed to open video source: {self.source}",
                    'fps': "0",
                    'detection_time_ms': "0",
                    'traffic_light_color': {"color": "unknown", "confidence": 0.0}
                })
                return
                
            # Configure frame timing based on source FPS
            frame_time = 1.0 / self.source_fps if self.source_fps > 0 else 0.033
            prev_time = time.time()
            
            # Log successful opening
            print(f"SUCCESS: Video source opened: {self.source}")
            print(f"Source info - FPS: {self.source_fps}, Size: {self.frame_width}x{self.frame_height}")
              # Main processing loop
            frame_error_count = 0
            max_consecutive_errors = 10
            
            while self._running and cap.isOpened():
                try:
                    ret, frame = cap.read()
                    # Add critical frame debugging
                    print(f"🟡 Frame read attempt: ret={ret}, frame={None if frame is None else frame.shape}")
                    
                    if not ret or frame is None:
                        frame_error_count += 1
                        print(f"⚠️ Frame read error ({frame_error_count}/{max_consecutive_errors})")
                        
                        if frame_error_count >= max_consecutive_errors:
                            print("❌ Too many consecutive frame errors, stopping video thread")
                            break
                            
                        # Skip this iteration and try again
                        time.sleep(0.1)  # Wait a bit before trying again
                        continue
                    
                    # Reset the error counter if we successfully got a frame
                    frame_error_count = 0
                except Exception as e:
                    print(f"❌ Critical error reading frame: {e}")
                    frame_error_count += 1
                    if frame_error_count >= max_consecutive_errors:
                        print("❌ Too many errors, stopping video thread")
                        break
                    continue
                    
                # Detection and violation processing
                process_start = time.time()
                
                # Process detections
                detection_start = time.time()
                detections = []
                if self.model_manager:
                    detections = self.model_manager.detect(frame)
                    print("[DEBUG] Raw detections:")
                    for det in detections:
                        print(f"  class_name: {det.get('class_name')}, class_id: {det.get('class_id')}, confidence: {det.get('confidence')}")
                    
                    # Normalize class names for consistency and check for traffic lights
                    traffic_light_indices = []
                    for i, det in enumerate(detections):
                        if 'class_name' in det:
                            original_name = det['class_name']
                            normalized_name = normalize_class_name(original_name)
                            
                            # Keep track of traffic light indices
                            if normalized_name == 'traffic light' or original_name == 'traffic light':
                                traffic_light_indices.append(i)
                                
                            if original_name != normalized_name:
                                print(f"📊 Normalized class name: '{original_name}' -> '{normalized_name}'")
                                
                            det['class_name'] = normalized_name
                            
                    # Ensure we have at least one traffic light for debugging
                    if not traffic_light_indices and self.source_type == 'video':
                        print("⚠️ No traffic lights detected, checking for objects that might be traffic lights...")
                        
                        # Try lowering the confidence threshold specifically for traffic lights
                        # This is only for debugging purposes
                        if self.model_manager and hasattr(self.model_manager, 'detect'):
                            try:
                                low_conf_detections = self.model_manager.detect(frame, conf_threshold=0.2)
                                for det in low_conf_detections:
                                    if 'class_name' in det and det['class_name'] == 'traffic light':
                                        if det not in detections:
                                            print(f"🚦 Found low confidence traffic light: {det['confidence']:.2f}")
                                            detections.append(det)
                            except:
                                pass
                            
                detection_time = (time.time() - detection_start) * 1000
                
                # Violation detection is disabled
                violation_start = time.time()
                violations = []
                # if self.model_manager and detections:
                #     violations = self.model_manager.detect_violations(
                #         detections, frame, time.time()
                #     )
                violation_time = (time.time() - violation_start) * 1000
                
                # Update tracking if available
                if self.model_manager:
                    detections = self.model_manager.update_tracking(detections, frame)
                    # If detections are returned as tuples, convert to dicts for downstream code
                    if detections and isinstance(detections[0], tuple):
                        # Convert (id, bbox, conf, class_id) to dict
                        detections = [
                            {'id': d[0], 'bbox': d[1], 'confidence': d[2], 'class_id': d[3]}
                            for d in detections
                        ]
                
                # Calculate timing metrics
                process_time = (time.time() - process_start) * 1000
                self.processing_times.append(process_time)
                
                # Update FPS
                now = time.time()
                self.frame_count += 1
                elapsed = now - self.start_time
                if elapsed > 0:
                    self.actual_fps = self.frame_count / elapsed
                    
                fps_smoothed = 1.0 / (now - prev_time) if now > prev_time else 0
                prev_time = now
                  # Update metrics
                self.performance_metrics = {
                    'FPS': f"{fps_smoothed:.1f}",
                    'Detection (ms)': f"{detection_time:.1f}",
                    'Total (ms)': f"{process_time:.1f}"
                }
                
                # Store current frame data (thread-safe)
                self.mutex.lock()
                self.current_frame = frame.copy()
                self.current_detections = detections
                self.mutex.unlock()
                  # Process frame with annotations before sending to UI
                annotated_frame = frame.copy()
                
                # --- VIOLATION DETECTION LOGIC (Run BEFORE drawing boxes) ---
                # First get violation information so we can color boxes appropriately
                violating_vehicle_ids = set()  # Track which vehicles are violating
                violations = []
                
                # Initialize traffic light variables
                traffic_lights = []
                has_traffic_lights = False
                
                # Handle multiple traffic lights with consensus approach
                traffic_light_count = 0
                for det in detections:
                    if is_traffic_light(det.get('class_name')):
                        has_traffic_lights = True
                        traffic_light_count += 1
                        if 'traffic_light_color' in det:
                            light_info = det['traffic_light_color']
                            traffic_lights.append({'bbox': det['bbox'], 'color': light_info.get('color', 'unknown'), 'confidence': light_info.get('confidence', 0.0)})
                
                print(f"[TRAFFIC LIGHT] Detected {traffic_light_count} traffic light(s), has_traffic_lights={has_traffic_lights}")
                if has_traffic_lights:
                    print(f"[TRAFFIC LIGHT] Traffic light colors: {[tl.get('color', 'unknown') for tl in traffic_lights]}")
                
                # Get traffic light position for crosswalk detection
                traffic_light_position = None
                if has_traffic_lights:
                    for det in detections:
                        if is_traffic_light(det.get('class_name')) and 'bbox' in det:
                            traffic_light_bbox = det['bbox']
                            # Extract center point from bbox for crosswalk utils
                            x1, y1, x2, y2 = traffic_light_bbox
                            traffic_light_position = ((x1 + x2) // 2, (y1 + y2) // 2)
                            break

                # Run crosswalk detection ONLY if traffic light is detected
                crosswalk_bbox, violation_line_y, debug_info = None, None, {}
                if has_traffic_lights and traffic_light_position is not None:
                    try:
                        print(f"[CROSSWALK] Traffic light detected at {traffic_light_position}, running crosswalk detection")
                        # Use new crosswalk_utils2 logic only when traffic light exists
                        annotated_frame, crosswalk_bbox, violation_line_y, debug_info = detect_crosswalk_and_violation_line(
                            annotated_frame,
                            traffic_light_position=traffic_light_position
                        )
                        print(f"[CROSSWALK] Detection result: crosswalk_bbox={crosswalk_bbox is not None}, violation_line_y={violation_line_y}")
                        # --- Draw crosswalk region if detected and close to traffic light ---
                        # (REMOVED: Do not draw crosswalk box or label)
                        # if crosswalk_bbox is not None:
                        #     x, y, w, h = map(int, crosswalk_bbox)
                        #     tl_x, tl_y = traffic_light_position
                        #     crosswalk_center_y = y + h // 2
                        #     distance = abs(crosswalk_center_y - tl_y)
                        #     print(f"[CROSSWALK DEBUG] Crosswalk bbox: {crosswalk_bbox}, Traffic light: {traffic_light_position}, vertical distance: {distance}")
                        #     if distance < 120:
                        #         cv2.rectangle(annotated_frame, (x, y), (x + w, y + h), (0, 255, 0), 3)
                        #         cv2.putText(annotated_frame, "Crosswalk", (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                        #     # Top and bottom edge of crosswalk
                        #     top_edge = y
                        #     bottom_edge = y + h
                        #     if abs(tl_y - top_edge) < abs(tl_y - bottom_edge):
                        #         crosswalk_edge_y = top_edge
                        #     else:
                        #         crosswalk_edge_y = bottom_edge
                        if crosswalk_bbox is not None:
                            x, y, w, h = map(int, crosswalk_bbox)
                            tl_x, tl_y = traffic_light_position
                            crosswalk_center_y = y + h // 2
                            distance = abs(crosswalk_center_y - tl_y)
                            print(f"[CROSSWALK DEBUG] Crosswalk bbox: {crosswalk_bbox}, Traffic light: {traffic_light_position}, vertical distance: {distance}")
                            # Top and bottom edge of crosswalk
                            top_edge = y
                            bottom_edge = y + h
                            if abs(tl_y - top_edge) < abs(tl_y - bottom_edge):
                                crosswalk_edge_y = top_edge
                            else:
                                crosswalk_edge_y = bottom_edge
                    except Exception as e:
                        print(f"[ERROR] Crosswalk detection failed: {e}")
                        crosswalk_bbox, violation_line_y, debug_info = None, None, {}
                else:
                    print(f"[CROSSWALK] No traffic light detected (has_traffic_lights={has_traffic_lights}), skipping crosswalk detection")
                    # NO crosswalk detection without traffic light
                    violation_line_y = None
                
                # Check if crosswalk is detected
                crosswalk_detected = crosswalk_bbox is not None
                stop_line_detected = debug_info.get('stop_line') is not None
                
                # ALWAYS process vehicle tracking (moved outside violation logic)
                tracked_vehicles = []
                if hasattr(self, 'vehicle_tracker') and self.vehicle_tracker is not None:
                    try:
                        # Filter vehicle detections
                        vehicle_classes = ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']
                        vehicle_dets = []
                        h, w = frame.shape[:2]
                        
                        print(f"[TRACK DEBUG] Processing {len(detections)} total detections")
                        
                        for det in detections:
                            if (det.get('class_name') in vehicle_classes and 
                                'bbox' in det and 
                                det.get('confidence', 0) > self.min_confidence_threshold):
                                
                                # Check bbox dimensions
                                bbox = det['bbox']
                                x1, y1, x2, y2 = bbox
                                box_w, box_h = x2-x1, y2-y1
                                box_area = box_w * box_h
                                area_ratio = box_area / (w * h)
                                
                                print(f"[TRACK DEBUG] Vehicle {det.get('class_name')} conf={det.get('confidence'):.2f}, area_ratio={area_ratio:.4f}")
                                
                                if 0.001 <= area_ratio <= 0.25:
                                    vehicle_dets.append(det)
                                    print(f"[TRACK DEBUG] Added vehicle: {det.get('class_name')} conf={det.get('confidence'):.2f}")
                                else:
                                    print(f"[TRACK DEBUG] Rejected vehicle: area_ratio={area_ratio:.4f} not in range [0.001, 0.25]")
                        
                        print(f"[TRACK DEBUG] Filtered to {len(vehicle_dets)} vehicle detections")
                        
                        # Update tracker
                        if len(vehicle_dets) > 0:
                            print(f"[TRACK DEBUG] Updating tracker with {len(vehicle_dets)} vehicles...")
                            tracks = self.vehicle_tracker.update(vehicle_dets, frame)
                            # Filter out tracks without bbox to avoid warnings
                            valid_tracks = []
                            for track in tracks:
                                bbox = None
                                if isinstance(track, dict):
                                    bbox = track.get('bbox', None)
                                else:
                                    bbox = getattr(track, 'bbox', None)
                                if bbox is not None:
                                    valid_tracks.append(track)
                                else:
                                    print(f"Warning: Track has no bbox, skipping: {track}")
                            tracks = valid_tracks
                            print(f"[TRACK DEBUG] Tracker returned {len(tracks)} tracks (after bbox filter)")
                        else:
                            print(f"[TRACK DEBUG] No vehicles to track, skipping tracker update")
                            tracks = []
                        
                        # Process each tracked vehicle
                        tracked_vehicles = []
                        track_ids_seen = []
                        
                        for track in tracks:
                            track_id = track['id']
                            bbox = track['bbox']
                            x1, y1, x2, y2 = map(float, bbox)
                            center_y = (y1 + y2) / 2
                            
                            # Check for duplicate IDs
                            if track_id in track_ids_seen:
                                print(f"[TRACK ERROR] Duplicate ID detected: {track_id}")
                            track_ids_seen.append(track_id)
                            
                            print(f"[TRACK DEBUG] Processing track ID={track_id} bbox={bbox}")
                            
                            # Initialize or update vehicle history
                            if track_id not in self.vehicle_history:
                                from collections import deque
                                self.vehicle_history[track_id] = deque(maxlen=self.position_history_size)
                            
                            # Initialize vehicle status if not exists
                            if track_id not in self.vehicle_statuses:
                                self.vehicle_statuses[track_id] = {
                                    'recent_movement': [],
                                    'violation_history': [],
                                    'crossed_during_red': False,
                                    'last_position': None,  # Track last position for jump detection
                                    'suspicious_jumps': 0   # Count suspicious position jumps
                                }
                            
                            # Detect suspicious position jumps (potential ID switches)
                            if self.vehicle_statuses[track_id]['last_position'] is not None:
                                last_y = self.vehicle_statuses[track_id]['last_position']
                                center_y = (y1 + y2) / 2
                                position_jump = abs(center_y - last_y)
                                
                                if position_jump > self.max_position_jump:
                                    self.vehicle_statuses[track_id]['suspicious_jumps'] += 1
                                    print(f"[TRACK WARNING] Vehicle ID={track_id} suspicious position jump: {last_y:.1f} -> {center_y:.1f} (jump={position_jump:.1f})")
                                    
                                    # If too many suspicious jumps, reset violation status to be safe
                                    if self.vehicle_statuses[track_id]['suspicious_jumps'] > 2:
                                        print(f"[TRACK RESET] Vehicle ID={track_id} has too many suspicious jumps, resetting violation status")
                                        self.vehicle_statuses[track_id]['crossed_during_red'] = False
                                        self.vehicle_statuses[track_id]['suspicious_jumps'] = 0
                            
                            # Update position history and last position
                            self.vehicle_history[track_id].append(center_y)
                            self.vehicle_statuses[track_id]['last_position'] = center_y
                            
                            # BALANCED movement detection - detect clear movement while avoiding false positives
                            is_moving = False
                            movement_detected = False
                            
                            if len(self.vehicle_history[track_id]) >= 3:  # Require at least 3 frames for movement detection
                                recent_positions = list(self.vehicle_history[track_id])
                                
                                # Check movement over 3 frames for quick response
                                if len(recent_positions) >= 3:
                                    movement_3frames = abs(recent_positions[-1] - recent_positions[-3])
                                    if movement_3frames > self.movement_threshold:  # More responsive threshold
                                        movement_detected = True
                                        print(f"[MOVEMENT] Vehicle ID={track_id} MOVING: 3-frame movement = {movement_3frames:.1f}")
                                
                                # Confirm with longer movement for stability (if available)
                                if len(recent_positions) >= 5:
                                    movement_5frames = abs(recent_positions[-1] - recent_positions[-5])
                                    if movement_5frames > self.movement_threshold * 1.5:  # Moderate threshold for 5 frames
                                        movement_detected = True
                                        print(f"[MOVEMENT] Vehicle ID={track_id} MOVING: 5-frame movement = {movement_5frames:.1f}")
                            
                            # Store historical movement for smoothing - require consistent movement
                            self.vehicle_statuses[track_id]['recent_movement'].append(movement_detected)
                            if len(self.vehicle_statuses[track_id]['recent_movement']) > 4:  # Shorter history for quicker response
                                self.vehicle_statuses[track_id]['recent_movement'].pop(0)
                            
                            # BALANCED: Require majority of recent frames to show movement (2 out of 4)
                            recent_movement_count = sum(self.vehicle_statuses[track_id]['recent_movement'])
                            total_recent_frames = len(self.vehicle_statuses[track_id]['recent_movement'])
                            if total_recent_frames >= 2 and recent_movement_count >= (total_recent_frames * 0.5):  # 50% of frames must show movement
                                is_moving = True
                            
                            print(f"[TRACK DEBUG] Vehicle ID={track_id} is_moving={is_moving} (threshold={self.movement_threshold})")
                            
                            # Initialize as not violating
                            is_violation = False
                            
                            tracked_vehicles.append({
                                'id': track_id,
                                'bbox': bbox,
                                'center_y': center_y,
                                'is_moving': is_moving,
                                'is_violation': is_violation
                            })
                        
                        print(f"[DEBUG] ByteTrack tracked {len(tracked_vehicles)} vehicles")
                        for i, tracked in enumerate(tracked_vehicles):
                            print(f"  Vehicle {i}: ID={tracked['id']}, center_y={tracked['center_y']:.1f}, moving={tracked['is_moving']}, violating={tracked['is_violation']}")
                        
                        # DEBUG: Print all tracked vehicle IDs and their bboxes for this frame
                        if tracked_vehicles:
                            print(f"[DEBUG] All tracked vehicles this frame:")
                            for v in tracked_vehicles:
                                print(f"    ID={v['id']} bbox={v['bbox']} center_y={v.get('center_y', 'NA')}")
                        else:
                            print("[DEBUG] No tracked vehicles this frame!")
                        
                        # Clean up old vehicle data
                        current_track_ids = [tracked['id'] for tracked in tracked_vehicles]
                        self._cleanup_old_vehicle_data(current_track_ids)
                        
                    except Exception as e:
                        print(f"[ERROR] Vehicle tracking failed: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    print("[WARN] ByteTrack vehicle tracker not available!")
                
                # Process violations - CHECK VEHICLES THAT CROSS THE LINE OVER A WINDOW OF FRAMES
                # IMPORTANT: Only process violations if traffic light is detected AND violation line exists
                if has_traffic_lights and violation_line_y is not None and tracked_vehicles:
                    print(f"[VIOLATION DEBUG] Traffic light present, checking {len(tracked_vehicles)} vehicles against violation line at y={violation_line_y}")
                    
                    # Check each tracked vehicle for violations
                    for tracked in tracked_vehicles:
                        track_id = tracked['id']
                        center_y = tracked['center_y']
                        is_moving = tracked['is_moving']
                        
                        # Get position history for this vehicle
                        position_history = list(self.vehicle_history[track_id])
                        
                        # Enhanced crossing detection: check over a window of frames
                        line_crossed_in_window = False
                        crossing_details = None
                        
                        if len(position_history) >= 2:
                            # Check for crossing over the last N frames (configurable window)
                            window_size = min(self.crossing_check_window, len(position_history))
                            
                            for i in range(1, window_size):
                                prev_y = position_history[-(i+1)]  # Earlier position
                                curr_y = position_history[-i]     # Later position
                                
                                # Check if vehicle crossed the line in this frame pair
                                if prev_y < violation_line_y and curr_y >= violation_line_y:
                                    line_crossed_in_window = True
                                    crossing_details = {
                                        'frames_ago': i,
                                        'prev_y': prev_y,
                                        'curr_y': curr_y,
                                        'window_checked': window_size
                                    }
                                    print(f"[VIOLATION DEBUG] Vehicle ID={track_id} crossed line {i} frames ago: {prev_y:.1f} -> {curr_y:.1f}")
                                    break
                        
                        # Check if traffic light is red
                        is_red_light = self.latest_traffic_light and self.latest_traffic_light.get('color') == 'red'
                        
                        print(f"[VIOLATION DEBUG] Vehicle ID={track_id}: latest_traffic_light={self.latest_traffic_light}, is_red_light={is_red_light}")
                        print(f"[VIOLATION DEBUG] Vehicle ID={track_id}: position_history={[f'{p:.1f}' for p in position_history[-5:]]}");  # Show last 5 positions
                        print(f"[VIOLATION DEBUG] Vehicle ID={track_id}: line_crossed_in_window={line_crossed_in_window}, crossing_details={crossing_details}")
                        
                        # Enhanced violation detection: vehicle crossed the line while moving and light is red
                        actively_crossing = (line_crossed_in_window and is_moving and is_red_light)
                        
                        # Initialize violation status for new vehicles
                        if 'crossed_during_red' not in self.vehicle_statuses[track_id]:
                            self.vehicle_statuses[track_id]['crossed_during_red'] = False
                        
                        # Mark vehicle as having crossed during red if it actively crosses
                        if actively_crossing:
                            # Additional validation: ensure it's not a false positive from ID switch
                            suspicious_jumps = self.vehicle_statuses[track_id].get('suspicious_jumps', 0)
                            if suspicious_jumps <= 1:  # Allow crossing if not too many suspicious jumps
                                self.vehicle_statuses[track_id]['crossed_during_red'] = True
                                print(f"[VIOLATION ALERT] Vehicle ID={track_id} CROSSED line during red light!")
                                print(f"  -> Crossing details: {crossing_details}")
                            else:
                                print(f"[VIOLATION IGNORED] Vehicle ID={track_id} crossing ignored due to {suspicious_jumps} suspicious jumps")
                        
                        # IMPORTANT: Reset violation status when light turns green (regardless of position)
                        if not is_red_light:
                            if self.vehicle_statuses[track_id]['crossed_during_red']:
                                print(f"[VIOLATION RESET] Vehicle ID={track_id} violation status reset (light turned green)")
                            self.vehicle_statuses[track_id]['crossed_during_red'] = False
                        
                        # Vehicle is violating ONLY if it crossed during red and light is still red
                        is_violation = (self.vehicle_statuses[track_id]['crossed_during_red'] and is_red_light)
                        
                        # Track current violation state for analytics - only actual crossings
                        self.vehicle_statuses[track_id]['violation_history'].append(actively_crossing)
                        if len(self.vehicle_statuses[track_id]['violation_history']) > 5:
                            self.vehicle_statuses[track_id]['violation_history'].pop(0)
                        
                        print(f"[VIOLATION DEBUG] Vehicle ID={track_id}: center_y={center_y:.1f}, line={violation_line_y}")
                        print(f"  history_window={[f'{p:.1f}' for p in position_history[-self.crossing_check_window:]]}")
                        print(f"  moving={is_moving}, red_light={is_red_light}")
                        print(f"  actively_crossing={actively_crossing}, crossed_during_red={self.vehicle_statuses[track_id]['crossed_during_red']}")
                        print(f"  suspicious_jumps={self.vehicle_statuses[track_id].get('suspicious_jumps', 0)}")
                        print(f"  FINAL_VIOLATION={is_violation}")
                        
                        # Update violation status
                        tracked['is_violation'] = is_violation
                        
                        if actively_crossing and self.vehicle_statuses[track_id].get('suspicious_jumps', 0) <= 1:  # Only add if not too many suspicious jumps
                            # Add to violating vehicles set
                            violating_vehicle_ids.add(track_id)
                            
                            # Add to violations list
                            timestamp = datetime.now()  # Keep as datetime object, not string
                            violations.append({
                                'track_id': track_id,
                                'id': track_id,
                                'bbox': [int(tracked['bbox'][0]), int(tracked['bbox'][1]), int(tracked['bbox'][2]), int(tracked['bbox'][3])],
                                'violation': 'line_crossing',
                                'violation_type': 'line_crossing',  # Add this for analytics compatibility
                                'timestamp': timestamp,
                                'line_position': violation_line_y,
                                'movement': crossing_details if crossing_details else {'prev_y': center_y, 'current_y': center_y},
                                'crossing_window': self.crossing_check_window,
                                'position_history': list(position_history[-10:])  # Include recent history for debugging
                            })
                            
                            print(f"[DEBUG] 🚨 VIOLATION DETECTED: Vehicle ID={track_id} CROSSED VIOLATION LINE")
                            print(f"    Enhanced detection: {crossing_details}")
                            print(f"    Position history: {[f'{p:.1f}' for p in position_history[-10:]]}")
                            print(f"    Detection window: {self.crossing_check_window} frames")
                            print(f"    while RED LIGHT & MOVING")
                
                # Emit progress signal after processing each frame
                if hasattr(self, 'progress_ready'):
                    self.progress_ready.emit(int(cap.get(cv2.CAP_PROP_POS_FRAMES)), int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), time.time())
                
                # Draw detections with bounding boxes - NOW with violation info
                # Only show traffic light and vehicle classes
                allowed_classes = ['traffic light', 'car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']
                filtered_detections = [det for det in detections if det.get('class_name') in allowed_classes]
                print(f"Drawing {len(filtered_detections)} detection boxes on frame (filtered)")
                
                # Statistics for debugging (always define, even if no detections)
                vehicles_with_ids = 0
                vehicles_without_ids = 0
                vehicles_moving = 0
                vehicles_violating = 0

                if detections and len(detections) > 0:
                    # Only show traffic light and vehicle classes
                    allowed_classes = ['traffic light', 'car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']
                    filtered_detections = [det for det in detections if det.get('class_name') in allowed_classes]
                    print(f"Drawing {len(filtered_detections)} detection boxes on frame (filtered)")
                    
                    # Statistics for debugging
                    vehicles_with_ids = 0
                    vehicles_without_ids = 0
                    vehicles_moving = 0
                    vehicles_violating = 0
                    
                    for det in filtered_detections:
                        if 'bbox' in det:
                            bbox = det['bbox']
                            x1, y1, x2, y2 = map(int, bbox)
                            label = det.get('class_name', 'object')
                            confidence = det.get('confidence', 0.0)
                            
                            # Robustness: ensure label and confidence are not None
                            if label is None:
                                label = 'object'
                            if confidence is None:
                                confidence = 0.0
                            class_id = det.get('class_id', -1)
                            
                            # Check if this detection corresponds to a violating or moving vehicle
                            det_center_x = (x1 + x2) / 2
                            det_center_y = (y1 + y2) / 2
                            is_violating_vehicle = False
                            is_moving_vehicle = False
                            vehicle_id = None
                            
                            # Match detection with tracked vehicles - IMPROVED MATCHING
                            if label in ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle'] and len(tracked_vehicles) > 0:
                                print(f"[MATCH DEBUG] Attempting to match {label} detection at ({det_center_x:.1f}, {det_center_y:.1f}) with {len(tracked_vehicles)} tracked vehicles")
                                best_match = None
                                best_distance = float('inf')
                                best_iou = 0.0
                                
                                for i, tracked in enumerate(tracked_vehicles):
                                    track_bbox = tracked['bbox']
                                    track_x1, track_y1, track_x2, track_y2 = map(float, track_bbox)
                                    
                                    # Calculate center distance
                                    track_center_x = (track_x1 + track_x2) / 2
                                    track_center_y = (track_y1 + track_y2) / 2
                                    center_distance = ((det_center_x - track_center_x)**2 + (det_center_y - track_center_y)**2)**0.5
                                    
                                    # Calculate IoU (Intersection over Union)
                                    intersection_x1 = max(x1, track_x1)
                                    intersection_y1 = max(y1, track_y1)
                                    intersection_x2 = min(x2, track_x2)
                                    intersection_y2 = min(y2, track_y2)
                                    
                                    if intersection_x2 > intersection_x1 and intersection_y2 > intersection_y1:
                                        intersection_area = (intersection_x2 - intersection_x1) * (intersection_y2 - intersection_y1)
                                        det_area = (x2 - x1) * (y2 - y1)
                                        track_area = (track_x2 - track_x1) * (track_y2 - track_y1)
                                        union_area = det_area + track_area - intersection_area
                                        iou = intersection_area / union_area if union_area > 0 else 0
                                    else:
                                        iou = 0
                                    
                                    print(f"[MATCH DEBUG] Track {i}: ID={tracked['id']}, center=({track_center_x:.1f}, {track_center_y:.1f}), distance={center_distance:.1f}, IoU={iou:.3f}")
                                    
                                    # Use stricter matching criteria - prioritize IoU over distance
                                    # Good match if: high IoU OR close center distance with some overlap
                                    is_good_match = (iou > 0.3) or (center_distance < 60 and iou > 0.1)
                                    
                                    if is_good_match:
                                        print(f"[MATCH DEBUG] Track {i} is a good match (IoU={iou:.3f}, distance={center_distance:.1f})")
                                        # Prefer higher IoU, then lower distance
                                        match_score = iou + (100 - min(center_distance, 100)) / 100  # Composite score
                                        if iou > best_iou or (iou == best_iou and center_distance < best_distance):
                                            best_distance = center_distance
                                            best_iou = iou
                                            best_match = tracked
                                    else:
                                        print(f"[MATCH DEBUG] Track {i} failed matching criteria (IoU={iou:.3f}, distance={center_distance:.1f})")
                                
                                if best_match:
                                    vehicle_id = best_match['id']
                                    is_moving_vehicle = best_match.get('is_moving', False)
                                    is_violating_vehicle = best_match.get('is_violation', False)
                                    print(f"[MATCH SUCCESS] Detection at ({det_center_x:.1f},{det_center_y:.1f}) matched with track ID={vehicle_id}")
                                    print(f"  -> STATUS: moving={is_moving_vehicle}, violating={is_violating_vehicle}, IoU={best_iou:.3f}, distance={best_distance:.1f}")
                                else:
                                    print(f"[MATCH FAILED] No suitable match found for {label} detection at ({det_center_x:.1f}, {det_center_y:.1f})")
                                    print(f"  -> Will draw as untracked detection with default color")
                            else:
                                if label not in ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']:
                                    print(f"[MATCH DEBUG] Skipping matching for non-vehicle label: {label}")
                                elif len(tracked_vehicles) == 0:
                                    print(f"[MATCH DEBUG] No tracked vehicles available for matching")
                                else:
                                    try:
                                        if len(tracked_vehicles) > 0:
                                            distances = [((det_center_x - (t['bbox'][0] + t['bbox'][2])/2)**2 + (det_center_y - (t['bbox'][1] + t['bbox'][3])/2)**2)**0.5 for t in tracked_vehicles[:3]]
                                            print(f"[DEBUG] No match found for detection at ({det_center_x:.1f},{det_center_y:.1f}) - distances: {distances}")
                                        else:
                                            print(f"[DEBUG] No tracked vehicles available to match detection at ({det_center_x:.1f},{det_center_y:.1f})")
                                    except NameError:
                                        print(f"[DEBUG] No match found for detection (coords unavailable)")
                                        if len(tracked_vehicles) > 0:
                                            print(f"[DEBUG] Had {len(tracked_vehicles)} tracked vehicles available")
                            
                            # Choose box color based on vehicle status 
                            # PRIORITY: 1. Violating (RED) - crossed during red light 2. Moving (ORANGE) 3. Stopped (GREEN)
                            if is_violating_vehicle and vehicle_id is not None:
                                box_color = (0, 0, 255)  # RED for violating vehicles (crossed line during red)
                                label_text = f"{label}:ID{vehicle_id}⚠️"
                                thickness = 4
                                vehicles_violating += 1
                                print(f"[COLOR DEBUG] Drawing RED box for VIOLATING vehicle ID={vehicle_id} (crossed during red)")
                            elif is_moving_vehicle and vehicle_id is not None and not is_violating_vehicle:
                                box_color = (0, 165, 255)  # ORANGE for moving vehicles (not violating)
                                label_text = f"{label}:ID{vehicle_id}"
                                thickness = 3
                                vehicles_moving += 1
                                print(f"[COLOR DEBUG] Drawing ORANGE box for MOVING vehicle ID={vehicle_id} (not violating)")
                            elif label in ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle'] and vehicle_id is not None:
                                box_color = (0, 255, 0)  # Green for stopped vehicles 
                                label_text = f"{label}:ID{vehicle_id}"
                                thickness = 2
                                print(f"[COLOR DEBUG] Drawing GREEN box for STOPPED vehicle ID={vehicle_id}")
                            elif is_traffic_light(label):
                                box_color = (0, 0, 255)  # Red for traffic lights
                                label_text = f"{label}"
                                thickness = 2
                            else:
                                box_color = (0, 255, 0)  # Default green for other objects
                                label_text = f"{label}"
                                thickness = 2
                            
                            # Update statistics
                            if label in ['car', 'truck', 'bus', 'motorcycle', 'van', 'bicycle']:
                                if vehicle_id is not None:
                                    vehicles_with_ids += 1
                                else:
                                    vehicles_without_ids += 1
                            
                            # Draw rectangle and label
                            cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), box_color, thickness)
                            cv2.putText(annotated_frame, label_text, (x1, y1-10), 
                                      cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 2)
                            #     id_text = f"ID: {det['id']}"
                            #     # Calculate text size for background
                            #     (tw, th), baseline = cv2.getTextSize(id_text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
                            #     # Draw filled rectangle for background (top-left of bbox)
                            #     cv2.rectangle(annotated_frame, (x1, y1 - th - 8), (x1 + tw + 4, y1), (0, 0, 0), -1)
                            #     # Draw the ID text in bold yellow
                            #     cv2.putText(annotated_frame, id_text, (x1 + 2, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
                            #     print(f"[DEBUG] Detection ID: {det['id']} BBOX: {bbox} CLASS: {label} CONF: {confidence:.2f}")
                           
                            if class_id == 9 or is_traffic_light(label):
                                try:
                                    light_info = detect_traffic_light_color(annotated_frame, [x1, y1, x2, y2])
                                    if light_info.get("color", "unknown") == "unknown":
                                        light_info = ensure_traffic_light_color(annotated_frame, [x1, y1, x2, y2])
                                    det['traffic_light_color'] = light_info
                                    # Draw enhanced traffic light status
                                    annotated_frame = draw_traffic_light_status(annotated_frame, bbox, light_info)
                                    
                                    # --- Update latest_traffic_light for UI/console ---
                                    self.latest_traffic_light = light_info
                                    
                                    # Add a prominent traffic light status at the top of the frame
                                    color = light_info.get('color', 'unknown')
                                    confidence = light_info.get('confidence', 0.0)
                                    
                                    if color == 'red':
                                        status_color = (0, 0, 255)  # Red
                                        status_text = f"Traffic Light: RED ({confidence:.2f})"
                                        
                                        # Draw a prominent red banner across the top
                                        banner_height = 40
                                        cv2.rectangle(annotated_frame, (0, 0), (annotated_frame.shape[1], banner_height), (0, 0, 150), -1)
                                        
                                        # Add text
                                        font = cv2.FONT_HERSHEY_DUPLEX
                                        font_scale = 0.9
                                        font_thickness = 2
                                        cv2.putText(annotated_frame, status_text, (10, banner_height-12), font, 
                                                  font_scale, (255, 255, 255), font_thickness)
                                except Exception as e:
                                    print(f"[WARN] Could not detect/draw traffic light color: {e}")

                # Print statistics summary
                print(f"[STATS] Vehicles: {vehicles_with_ids} with IDs, {vehicles_without_ids} without IDs")
                print(f"[STATS] Moving: {vehicles_moving}, Violating: {vehicles_violating}")
                
                # Handle multiple traffic lights with consensus approach
                for det in detections:
                    if is_traffic_light(det.get('class_name')):
                        has_traffic_lights = True
                        if 'traffic_light_color' in det:
                            light_info = det['traffic_light_color']
                            traffic_lights.append({'bbox': det['bbox'], 'color': light_info.get('color', 'unknown'), 'confidence': light_info.get('confidence', 0.0)})
                
                # Determine the dominant traffic light color based on confidence
                if traffic_lights:
                    # Filter to just red lights and sort by confidence
                    red_lights = [tl for tl in traffic_lights if tl.get('color') == 'red']
                    if red_lights:
                        # Use the highest confidence red light for display
                        highest_conf_red = max(red_lights, key=lambda x: x.get('confidence', 0))
                        # Update the global traffic light status for consistent UI display
                        self.latest_traffic_light = {
                            'color': 'red',
                            'confidence': highest_conf_red.get('confidence', 0.0)
                        }

                # Emit individual violation signals for each violation
                if violations:
                    for violation in violations:
                        print(f"🚨 Emitting RED LIGHT VIOLATION: Track ID {violation['track_id']}")
                        # Add additional data to the violation
                        violation['frame'] = frame
                        violation['violation_line_y'] = violation_line_y
                        self.violation_detected.emit(violation)
                    print(f"[DEBUG] Emitted {len(violations)} violation signals")
                
                # Add FPS display directly on frame
                # cv2.putText(annotated_frame, f"FPS: {fps_smoothed:.1f}", (10, 30), 
                #            cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

                # # --- Always draw detected traffic light color indicator at top ---
                # color = self.latest_traffic_light.get('color', 'unknown') if isinstance(self.latest_traffic_light, dict) else str(self.latest_traffic_light)
                # confidence = self.latest_traffic_light.get('confidence', 0.0) if isinstance(self.latest_traffic_light, dict) else 0.0
                # indicator_size = 30
                # margin = 10
                # status_colors = {
                #     "red": (0, 0, 255),
                #     "yellow": (0, 255, 255),
                #     "green": (0, 255, 0),
                #     "unknown": (200, 200, 200)
                # }
                # draw_color = status_colors.get(color, (200, 200, 200))
                # # Draw circle indicator
                # cv2.circle(
                #     annotated_frame,
                #     (annotated_frame.shape[1] - margin - indicator_size, margin + indicator_size),
                #     indicator_size,
                #     draw_color,
                #     -1
                # )
                # # Add color text
                # cv2.putText(
                #     annotated_frame,
                #     f"{color.upper()} ({confidence:.2f})",
                #     (annotated_frame.shape[1] - margin - indicator_size - 120, margin + indicator_size + 10),
                #     cv2.FONT_HERSHEY_SIMPLEX,
                #     0.7,
                #     (0, 0, 0),
                #     2
                # )

                # Signal for raw data subscribers (now without violations)
                # Emit with correct number of arguments
                try:
                    self.raw_frame_ready.emit(frame.copy(), detections, fps_smoothed)
                    print(f"✅ raw_frame_ready signal emitted with {len(detections)} detections, fps={fps_smoothed:.1f}")
                except Exception as e:
                    print(f"✅ raw_frame_ready signal emitted with {len(detections)} detections, fps={fps_smoothed:.1f}")
                except Exception as e:
                    print(f"❌ Error emitting raw_frame_ready: {e}")
                    import traceback
                    traceback.print_exc()
                
                # Emit the NumPy frame signal for direct display - annotated version for visual feedback
                print(f"🔴 Emitting frame_np_ready signal with annotated_frame shape: {annotated_frame.shape}")
                try:
                    # Make sure the frame can be safely transmitted over Qt's signal system
                    # Create a contiguous copy of the array
                    frame_copy = np.ascontiguousarray(annotated_frame)
                    print(f"🔍 Debug - Before emission: frame_copy type={type(frame_copy)}, shape={frame_copy.shape}, is_contiguous={frame_copy.flags['C_CONTIGUOUS']}")
                    self.frame_np_ready.emit(frame_copy)
                    print("✅ frame_np_ready signal emitted successfully")
                except Exception as e:
                    print(f"❌ Error emitting frame: {e}")
                    import traceback
                    traceback.print_exc()
                
                # Emit QPixmap for video detection tab (frame_ready)
                try:
                    from PySide6.QtGui import QImage, QPixmap
                    rgb_frame = cv2.cvtColor(annotated_frame, cv2.COLOR_BGR2RGB)
                    h, w, ch = rgb_frame.shape
                    bytes_per_line = ch * w
                    qimg = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
                    pixmap = QPixmap.fromImage(qimg)
                    metrics = {
                        'FPS': fps_smoothed,
                        'Detection (ms)': detection_time
                    }
                    self.frame_ready.emit(pixmap, detections, metrics)
                    print("✅ frame_ready signal emitted for video detection tab")
                except Exception as e:
                    print(f"❌ Error emitting frame_ready: {e}")
                    import traceback
                    traceback.print_exc()
                
                # Emit stats signal for performance monitoring
                stats = {
                    'fps': fps_smoothed,
                    'detection_fps': fps_smoothed,  # Numeric value for analytics
                    'detection_time': detection_time,
                    'detection_time_ms': detection_time,  # Numeric value for analytics
                    'traffic_light_color': self.latest_traffic_light,
                    'cars': sum(1 for d in detections if d.get('class_name', '').lower() == 'car'),
                    'trucks': sum(1 for d in detections if d.get('class_name', '').lower() == 'truck'),
                    'peds': sum(1 for d in detections if d.get('class_name', '').lower() in ['person', 'pedestrian', 'human']),
                    'model': getattr(self.inference_model, 'name', '-') if hasattr(self, 'inference_model') else '-',
                    'device': getattr(self.inference_model, 'device', '-') if hasattr(self, 'inference_model') else '-'
                }
                # Print detailed stats for debugging
                tl_color = "unknown"
                if isinstance(self.latest_traffic_light, dict):
                    tl_color = self.latest_traffic_light.get('color', 'unknown')
                elif isinstance(self.latest_traffic_light, str):
                    tl_color = self.latest_traffic_light
                print(f"🟢 Stats Updated: FPS={fps_smoothed:.2f}, Inference={detection_time:.2f}ms, Traffic Light={tl_color}")
                # Emit stats signal
                self.stats_ready.emit(stats)

                # --- Ensure analytics update every frame ---
                if hasattr(self, 'analytics_controller') and self.analytics_controller is not None:
                    try:
                        self.analytics_controller.process_frame_data(frame, detections, stats)
                        print("[DEBUG] Called analytics_controller.process_frame_data for analytics update")
                    except Exception as e:
                        print(f"[ERROR] Could not update analytics: {e}")
                
                # Control processing rate for file sources
                if isinstance(self.source, str) and self.source_fps > 0:
                    frame_duration = time.time() - process_start
                    if frame_duration < frame_time:
                        time.sleep(frame_time - frame_duration)
            
            cap.release()
        except Exception as e:
            print(f"Video processing error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._running = False
    def _process_frame(self):
        """Process current frame for display with improved error handling"""
        try:
            self.mutex.lock()
            if self.current_frame is None:
                now = time.time()
                if now - getattr(self, '_last_no_frame_log', 0) > 2:
                    print("⚠️ No frame available to process")
                    self._last_no_frame_log = now
                self.mutex.unlock()
                
                # Check if we're running - if not, this is expected behavior
                if not self._running:
                    return
                
                # If we are running but have no frame, create a blank frame with error message
                h, w = 480, 640  # Default size
                blank_frame = np.zeros((h, w, 3), dtype=np.uint8)
                cv2.putText(blank_frame, "No video input", (w//2-140, h//2), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                
                # Emit this blank frame
                try:
                    self.frame_np_ready.emit(blank_frame)
                except Exception as e:
                    print(f"Error emitting blank frame: {e}")
                return
            
            # Make a copy of the data we need
            try:
                frame = self.current_frame.copy()
                if self.current_detections is not None:
                    detections = self.current_detections.copy()
                else:
                    detections = []
                violations = []  # Violations are disabled
                metrics = self.performance_metrics.copy()
            except Exception as e:
                print(f"Error copying frame data: {e}")
                self.mutex.unlock()
                return
            self.mutex.unlock()
            
            # --- Frame processing logic (drawing, annotations, etc) ---
            # Draw FPS on frame
            if 'FPS' in metrics:
                cv2.putText(frame, f"FPS: {metrics['FPS']}", (10, 30), 
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            
            # Draw detections
            for det in detections:
                if 'bbox' in det:
                    bbox = det['bbox']
                    x1, y1, x2, y2 = map(int, bbox)
                    label = det.get('class_name', 'object')
                    confidence = det.get('confidence', 0.0)
                    
                    # Draw bounding box
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    
                    # Put label text
                    cv2.putText(frame, f"{label} ({confidence:.2f})", (x1, y1-10), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
            
            # --- END OF FRAME PROCESSING LOGIC ---
            
            # Emit the processed frame for display
            self.frame_np_ready.emit(frame)
        except Exception as e:
            print(f"Error in _process_frame: {e}")
        finally:
            self.mutex.unlock()

