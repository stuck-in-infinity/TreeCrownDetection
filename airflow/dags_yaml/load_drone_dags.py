"""Airflow DAG loader for the drone YAML files (dag-factory).

Keep the words "airflow" and "dag" here: Airflow skips files without them.
"""
from pathlib import Path

from dagfactory import load_yaml_dags

load_yaml_dags(
    globals_dict=globals(),
    dags_folder=str(Path(__file__).parent),
    suffix=["drone_analyze.yaml", "drone_finalize.yaml"],
)
