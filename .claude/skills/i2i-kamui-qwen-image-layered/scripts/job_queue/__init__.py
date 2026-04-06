# Default maximum number of poll attempts before timeout.
# With a default poll_interval of 2.0s, 3000 polls ≈ 100 minutes.
# Override via --max-polls CLI flag or by passing max_polls= to API functions.
DEFAULT_MAX_POLLS = 3000