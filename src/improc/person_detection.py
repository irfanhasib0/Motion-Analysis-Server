"""
Person Detection using OpenCV Haar Cascades

This module provides face and body detection using pre-trained Haar cascade classifiers.
The detected bounding boxes are returned in a format compatible with the optical flow tracker.

Features:
- Frontal face detection using Haar cascades
- Full body detection using Haar cascades
- Configurable detection parameters
- Returns boxes compatible with optical flow tracker format

Example Usage:
    detector = PersonDetector(enable_face=True, enable_body=True)
    boxes = detector.detect(frame)
"""

import cv2
import numpy as np

class PersonDetector:
    def __init__(self, 
                 enable_face=False, 
                 enable_body=False,
                 face_scale_factor=1.1,
                 face_min_neighbors=5,
                 body_scale_factor=1.1,
                 body_min_neighbors=3,
                 min_face_size=(30, 30),
                 min_body_size=(50, 50)):
        """
        Initialize person detector with Haar cascades
        
        Args:
            enable_face: Enable face detection
            enable_body: Enable body detection  
            face_scale_factor: Face detection scale factor
            face_min_neighbors: Minimum neighbors for face detection
            body_scale_factor: Body detection scale factor
            body_min_neighbors: Minimum neighbors for body detection
            min_face_size: Minimum face size (width, height)
            min_body_size: Minimum body size (width, height)
        """
        self.enable_face = enable_face
        self.enable_body = enable_body
        
        # Face detection parameters
        self.face_scale_factor = face_scale_factor
        self.face_min_neighbors = face_min_neighbors
        self.min_face_size = min_face_size
        
        # Body detection parameters  
        self.body_scale_factor = body_scale_factor
        self.body_min_neighbors = body_min_neighbors
        self.min_body_size = min_body_size
        
        # Initialize Haar cascade classifiers
        self.face_cascade = None
        self.body_cascade = None
        
        if self.enable_face:
            try:
                self.face_cascade = cv2.CascadeClassifier(
                    cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
                )
                if self.face_cascade.empty():
                    print("Warning: Face cascade failed to load")
                    self.enable_face = False
            except Exception as e:
                print(f"Warning: Could not load face cascade: {e}")
                self.enable_face = False
                
        if self.enable_body:
            try:
                self.body_cascade = cv2.CascadeClassifier(
                    cv2.data.haarcascades + 'haarcascade_fullbody.xml'
                )
                if self.body_cascade.empty():
                    print("Warning: Body cascade failed to load")
                    self.enable_body = False
            except Exception as e:
                print(f"Warning: Could not load body cascade: {e}")
                self.enable_body = False
    
    def detect(self, frame):
        """
        Detect faces and bodies in the frame
        
        Args:
            frame: Input BGR frame
            
        Returns:
            List of detection dictionaries compatible with optical flow tracker format
        """
        if not self.enable_face and not self.enable_body:
            return []
            
        detections = []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        # Face detection
        if self.enable_face and self.face_cascade is not None:
            faces = self.face_cascade.detectMultiScale(
                gray,
                scaleFactor=self.face_scale_factor,
                minNeighbors=self.face_min_neighbors,
                minSize=self.min_face_size,
                flags=cv2.CASCADE_SCALE_IMAGE
            )
            
            for (x, y, w, h) in faces:
                # Create mask for the face region
                mask = np.zeros(gray.shape, dtype=np.uint8)
                mask[y:y+h, x:x+w] = 255
                
                detection = {
                    'bbox': [x, y, x + w, y + h],
                    'bbox_xywh': [int(x + w/2), int(y + h/2), int(w), int(h)],
                    'centroid': [y + h / 2, x + w / 2],
                    'mask': mask,
                    'type': 'face'
                }
                detections.append(detection)
        
        # Body detection
        if self.enable_body and self.body_cascade is not None:
            bodies = self.body_cascade.detectMultiScale(
                gray,
                scaleFactor=self.body_scale_factor,
                minNeighbors=self.body_min_neighbors,
                minSize=self.min_body_size,
                flags=cv2.CASCADE_SCALE_IMAGE
            )
            
            for (x, y, w, h) in bodies:
                # Create mask for the body region
                mask = np.zeros(gray.shape, dtype=np.uint8)
                mask[y:y+h, x:x+w] = 255
                
                detection = {
                    'bbox': [x, y, x + w, y + h],
                    'bbox_xywh': [int(x + w/2), int(y + h/2), int(w), int(h)],
                    'centroid': [y + h / 2, x + w / 2],
                    'mask': mask,
                    'type': 'body'
                }
                detections.append(detection)
        
        return detections
    
    def set_face_enabled(self, enabled):
        """Enable or disable face detection"""
        self.enable_face = enabled and (self.face_cascade is not None)
    
    def set_body_enabled(self, enabled):
        """Enable or disable body detection"""
        self.enable_body = enabled and (self.body_cascade is not None)
    
    def is_face_enabled(self):
        """Check if face detection is enabled"""
        return self.enable_face
    
    def is_body_enabled(self):
        """Check if body detection is enabled"""  
        return self.enable_body