"""PowerPoint courseware analysis.

This package is intentionally separate from ``deep_document``. Deep extraction
emits a structural document object for any supported file type; courseware
analysis is an optional PowerPoint/training-content layer.
"""

from docling_serve.powerpoint_courseware.builder import build_course_artifacts

__all__ = ["build_course_artifacts"]
