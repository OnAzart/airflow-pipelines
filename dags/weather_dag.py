import json
from datetime import datetime
from time import strptime

from airflow import DAG
from airflow.models import xcom, Variable
from airflow.providers.http.operators.http import SimpleHttpOperator
from airflow.providers.sqlite.operators.sqlite import SqliteOperator
from airflow.utils.dates import days_ago

from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.providers.common.sql.operators.sql import BaseSQLOperator
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.utils.task_group import TaskGroup


def choosing_endpoint(city, **context):
    scheduled_date_to_run = context['execution_date']
    if scheduled_date_to_run.date() != datetime.today().date():
        return f'retrieve_weather_data_{city}'
    return f'retrieve_historical_weather_data_{city}'


def transforming_data(city, **context):
    print(context)
    ti = context['ti']
    info = ti.xcom_pull(task_ids=f'retrieve_weather_data_{city}') or \
           ti.xcom_pull(task_ids=f'retrieve_historical_weather_data_{city}')

    timestamp = info["dt"]
    temp = info["main"]["temp"]
    humidity = info["main"]["humidity"]
    cloudiness = info["weather"][0]["description"]
    wind_speed = info["wind"]["speed"]

    return timestamp, temp, humidity, cloudiness, wind_speed


with DAG(dag_id='weather_dag', schedule_interval='0 * * * *', start_date=days_ago(0), catchup=False) as dag:
    create_table = SqliteOperator(task_id='create_table',
                                  sqlite_conn_id='measure_db',
                                  sql='''CREATE TABLE IF NOT EXISTS measures_ext (
                                         city VARCHAR(20) NOT NULL,
                                         timestamp TIMESTAMP NOT NULL,
                                         temp FLOAT,
                                         humidity INT,
                                         cloudiness VARCHAR(25),
                                         wind_speed FLOAT
                                        );'''
                                  )

    cities = ['Lviv', 'Kyiv', 'Zhmerynka', 'Kharkiv', 'Odesa']
    for city in cities:
        # postgres://nazar:1111@localhost:5432/airflow-pipelines
        choose_endpoint = BranchPythonOperator(task_id=f'choose_endpoint_{city}',
                                               python_callable=choosing_endpoint,
                                               op_kwargs={'city': city})
        retrieve_weather_data = SimpleHttpOperator(task_id=f'retrieve_weather_data_{city}',
                                                   method='GET',
                                                   http_conn_id='weather_http_api',
                                                   endpoint='data/2.5/weather',
                                                   data={'q': f'{city}', 'appid': Variable.get('OPENWEATHER_API')},
                                                   response_filter=lambda x: json.loads(x.text),
                                                   log_response=True,
                                                   )

        retrieve_historical_weather_data = SimpleHttpOperator(task_id=f'retrieve_historical_weather_data_{city}',
                                                              method='GET',
                                                              http_conn_id='weather_http_api',
                                                              endpoint='data/3.0/onecall/timemachine',
                                                              response_filter=lambda x: json.loads(x.text),
                                                              log_response=True,
                                                              data={
                                                                  # 'q': f'{city}',
                                                                  'lat': '49.842957',
                                                                  'lon': '24.031111',
                                                                  'appid': Variable.get('OPENWEATHER_API'),
                                                                  'dt': '{{ execution_date.timestamp()|int }}'
                                                              }
                                                              )
        retrieve_weather_tasks = [retrieve_weather_data, retrieve_historical_weather_data]

        transform_data = PythonOperator(task_id=f'transform_data_{city}',
                                        python_callable=transforming_data,
                                        op_kwargs={'city': f'{city}'}
                                        )

        inject_data = SqliteOperator(
            task_id=f"inject_data_{city}",
            sqlite_conn_id="measure_db",
            sql=f"""
                INSERT INTO measures_ext (city, timestamp, temp, humidity, cloudiness, wind_speed) VALUES
                    ("{city}",
                    {{{{ti.xcom_pull(task_ids='transform_data_{city}')[0]}}}},
                    {{{{ti.xcom_pull(task_ids='transform_data_{city}')[1]}}}},
                    {{{{ti.xcom_pull(task_ids='transform_data_{city}')[2]}}}},
                    "{{{{ti.xcom_pull(task_ids='transform_data_{city}')[3]}}}}",
                    {{{{ti.xcom_pull(task_ids='transform_data_{city}')[4]}}}});
            """,
        )

        create_table >> choose_endpoint >> retrieve_weather_tasks >> transform_data >> inject_data
