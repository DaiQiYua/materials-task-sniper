# -*- coding: utf-8 -*-
"""pytest 路径引导：保证 server / task_sniper 可导入。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
