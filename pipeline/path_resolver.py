#!/usr/bin/env python3
"""
Path resolver for Video2STL pipeline.

Handles configuration loading and path resolution with per-category support.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml


class PathResolver:
    """Resolves paths and configuration for the Video2STL pipeline."""
    
    def __init__(self, config_path: str):
        self.config_path = Path(config_path).resolve()
        self.config_dir = self.config_path.parent
        
        with open(self.config_path) as f:
            self.config = yaml.safe_load(f)
        
        # Resolve base paths
        self.code_root = (self.config_dir / self.config['paths']['code_root']).resolve()
        self.data_root = (self.config_dir / self.config['paths']['data_root']).resolve()
        
        # Project paths
        project_name = self.config['project']['name']
        self.project_dir = self.data_root / "projects" / project_name
        
        # Run directory
        run_id = self.config['paths']['run_id']
        if run_id == "auto":
            from datetime import datetime
            run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
        self.run_id = run_id
        self.run_dir = self.project_dir / "runs" / run_id
        
        # STL templates — shared across projects (relative to data_root)
        stl_templates_rel = self.config['paths'].get('stl_templates', 'stl_templates')
        self.stl_templates_dir = self.data_root / stl_templates_rel
        
        # Shared input paths
        self._shared_inputs = self.config['paths'].get('input', {})
        
        # Defaults
        self._defaults = self.config.get('defaults', {})
    
    # ========================================================================
    # Category-aware path resolution
    # ========================================================================
    
    def get_category_config(self, category: str) -> Dict:
        """Get full configuration for a category, merging with defaults."""
        if category not in self.config.get('categories', {}):
            raise ValueError(f"Category '{category}' not found in config")
        
        cat_config = self.config['categories'][category].copy()
        
        # Merge each step config with defaults
        for step in ['segmentation', 'extraction', 'filtering', 'healing', 'scene']:
            default_step = self._defaults.get(step, {})
            cat_step = cat_config.get(step, {})
            # Defaults first, then category overrides
            merged = {**default_step, **cat_step}
            cat_config[step] = merged
        
        return cat_config
    
    def get_enabled_categories(self) -> List[str]:
        """Get list of enabled category names."""
        categories = self.config.get('categories', {})
        return [name for name, cfg in categories.items() if cfg.get('enabled', False)]
    
    # ========================================================================
    # Category-specific input paths
    # ========================================================================
    
    def get_frames_dir(self, category: str) -> Path:
        """Get input frames directory for a category."""
        cat_config = self.config['categories'].get(category, {})
        cat_input = cat_config.get('input', {})
        
        # Category-specific frames, or fall back to shared
        frames_rel = cat_input.get('frames') or self._shared_inputs.get('frames')
        if not frames_rel:
            raise ValueError(f"No frames path configured for category '{category}'")
        
        return self.project_dir / frames_rel
    
    def get_point_cloud_path(self, category: str = None) -> Path:
        """Get input point cloud path (usually shared across categories)."""
        cat_input = {}
        if category:
            cat_config = self.config['categories'].get(category, {})
            cat_input = cat_config.get('input', {})
        
        pc_rel = cat_input.get('point_cloud') or self._shared_inputs.get('point_cloud')
        if not pc_rel:
            raise ValueError("No point_cloud path configured")
        
        return self.project_dir / pc_rel
    
    def get_transforms_path(self, category: str = None) -> Path:
        """Get transforms.json path."""
        cat_input = {}
        if category:
            cat_config = self.config['categories'].get(category, {})
            cat_input = cat_config.get('input', {})
        
        tf_rel = cat_input.get('transforms') or self._shared_inputs.get('transforms')
        return self.project_dir / tf_rel if tf_rel else None
    
    def get_dataparser_path(self, category: str = None) -> Path:
        """Get dataparser_transforms.json path."""
        cat_input = {}
        if category:
            cat_config = self.config['categories'].get(category, {})
            cat_input = cat_config.get('input', {})
        
        dp_rel = cat_input.get('dataparser') or self._shared_inputs.get('dataparser')
        return self.project_dir / dp_rel if dp_rel else None
    
    # ========================================================================
    # Category-specific output paths
    # ========================================================================
    
    def get_segmentation_dir(self, category: str) -> Path:
        """Get segmentation output directory for a category."""
        return self.run_dir / "1_segmentation" / category
    
    def get_extracted_dir(self, category: str, subdir: str = "raw") -> Path:
        """Get extraction output directory for a category."""
        return self.run_dir / "2_extracted" / category / subdir
    
    def get_healed_dir(self, category: str, subdir: str = "merged_groups") -> Path:
        """Get healed output directory for a category."""
        return self.run_dir / "3_healed" / category / subdir
    
    def get_stl_dir(self, category: str = None) -> Path:
        """Get STL output directory (category-specific or combined)."""
        if category:
            return self.run_dir / "4_stl" / category
        return self.run_dir / "4_stl"
    
    # ========================================================================
    # Step parameter access
    # ========================================================================
    
    def get_step_config(self, category: str, step: str) -> Dict:
        """Get configuration for a specific step of a category."""
        cat_config = self.get_category_config(category)
        return cat_config.get(step, {})
    
    def get_param(self, category: str, step: str, param: str, default: Any = None) -> Any:
        """Get a specific parameter for a category's step."""
        step_config = self.get_step_config(category, step)
        return step_config.get(param, default)
    
    # ========================================================================
    # STL template paths
    # ========================================================================
    
    def get_stl_template_path(self, category: str) -> Path:
        """Get STL template file path for a category."""
        cat_config = self.config['categories'].get(category, {})
        placement = cat_config.get('placement', {})
        stl_file = placement.get('stl_file')

        if not stl_file:
            raise ValueError(f"No stl_file configured for category '{category}'")

        return self.stl_templates_dir / stl_file

    def list_stl_templates(self) -> List[str]:
        """List all available STL template files."""
        if not self.stl_templates_dir.exists():
            return []
        return sorted([f.name for f in self.stl_templates_dir.glob("*.stl")])
    
    # ========================================================================
    # Utility
    # ========================================================================
    
    def ensure_dirs(self, category: str):
        """Create all output directories for a category."""
        dirs = [
            self.get_segmentation_dir(category),
            self.get_extracted_dir(category, "raw"),
            self.get_extracted_dir(category, "filtered"),
            self.get_healed_dir(category, "merged_groups"),
            self.get_stl_dir(category),
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
    
    def ensure_all_dirs(self):
        """Create output directories for all enabled categories."""
        for category in self.get_enabled_categories():
            self.ensure_dirs(category)
        # Also ensure combined STL dir
        self.get_stl_dir().mkdir(parents=True, exist_ok=True)

    def copy_config_to_run_dir(self):
        """Copy the configuration file to the run directory with versioning.

        Saves config as config_v1.yaml, config_v2.yaml, etc. to preserve run history.
        """
        import shutil
        import re

        # Ensure run directory exists
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Find existing config versions
        existing_configs = list(self.run_dir.glob("config_v*.yaml"))

        # Extract version numbers
        version_pattern = re.compile(r'config_v(\d+)\.yaml')
        versions = []
        for config_file in existing_configs:
            match = version_pattern.match(config_file.name)
            if match:
                versions.append(int(match.group(1)))

        # Determine next version number
        next_version = max(versions) + 1 if versions else 1

        # Copy config file with version number
        dest_path = self.run_dir / f"config_v{next_version}.yaml"
        shutil.copy2(self.config_path, dest_path)

        return dest_path
    
    def print_summary(self):
        """Print configuration summary."""
        print(f"Project: {self.config['project']['name']}")
        print(f"Run ID: {self.run_id}")
        print(f"Run dir: {self.run_dir}")
        print(f"\nEnabled categories:")
        for cat in self.get_enabled_categories():
            cat_cfg = self.get_category_config(cat)
            prompt = cat_cfg.get('segmentation', {}).get('prompt', 'N/A')
            frames = self.get_frames_dir(cat)
            print(f"  - {cat}: prompt='{prompt}', frames={frames}")