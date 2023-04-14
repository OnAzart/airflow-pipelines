from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.utils.dates import days_ago


with DAG(dag_id='fst_dag', schedule_interval='@daily', start_date=days_ago(2), catchup=False) as dag:
    BashOperator(task_id='sleep', bash_command='sleep 5')
    BashOperator(task_id='sleep1', bash_command='sleep 4')
    BashOperator(task_id='sleep2', bash_command='sleep 6')
