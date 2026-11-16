from enum import Enum


class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'


class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'


class BatchOrigin(str, Enum):
    prepared = 'prepared'
    split = 'split'
    mixed = 'mixed'


class InspectionResult(str, Enum):
    pending = 'pending'
    passed = 'passed'
    failed = 'failed'


class TraceState(str, Enum):
    pending = 'pending'
    recovering = 'recovering'
    settled = 'settled'


class RecallStatus(str, Enum):
    scanning = 'scanning'
    completed = 'completed'


class RecoveryStatus(str, Enum):
    pending = 'pending'
    partial = 'partial'
    settled = 'settled'
