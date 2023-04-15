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
    if scheduled_date_to_run.date() == datetime.today().date():
        return f'retrive_data_group_{city}.retrieve_weather_data_{city}'
    return f'retrive_data_group_{city}.retrieve_historical_weather_data_{city}'


def transforming_data(city, **context):
    ti = context['ti']
    group_name = f'retrive_data_group_{city}'

    weather_data = ti.xcom_pull(task_ids=f'{group_name}.retrieve_weather_data_{city}')
    historical_weather_data = ti.xcom_pull(task_ids=f'{group_name}.retrieve_historical_weather_data_{city}')
    if weather_data:
        timestamp = weather_data["dt"]
        temp = weather_data["main"]["temp"]
        humidity = weather_data["main"]["humidity"]
        cloudiness = weather_data["weather"][0]["description"]
        wind_speed = weather_data["wind"]["speed"]
    elif historical_weather_data:
        timestamp = historical_weather_data["data"]["dt"]
        temp = historical_weather_data["data"]["temp"]
        humidity = historical_weather_data["data"]["humidity"]
        cloudiness = historical_weather_data["data"]["weather"]["description"]
        wind_speed = historical_weather_data["data"]["wind_speed"]
    else:
        # timestamp, temp, humidity, cloudiness, wind_speed = [None for _ in range(5)]
        raise Exception("No data for transform")
    return timestamp, temp, humidity, cloudiness, wind_speed


with DAG(dag_id='weather_dag', schedule_interval='* 23 * * *', start_date=days_ago(1), catchup=True) as dag:
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

    # cities = ['Lviv', 'Kyiv', 'Zhmerynka', 'Kharkiv', 'Odesa']
    cities_dict = {'Lviv': [49.8383, 24.0232], 'Kyiv':  [50.4333, 30.5167], 'Zhmerynka': [49.037, 28.112],
              'Kharkiv': [50, 36.25], 'Odesa': [46.4775, 30.7326]}
    for city, coordinates in cities_dict.items():
        choose_endpoint = BranchPythonOperator(task_id=f'choose_endpoint_{city}',
                                               python_callable=choosing_endpoint,
                                               op_kwargs={'city': city})

        with TaskGroup(group_id=f'retrive_data_group_{city}') as retrieve_data_group:
            retrieve_weather_data = SimpleHttpOperator(task_id=f'retrieve_weather_data_{city}',
                                                       method='GET',
                                                       http_conn_id='weather_http_api',
                                                       endpoint='data/2.5/weather',
                                                       data={'q': f'{city}',
                                                             'appid': Variable.get('OPENWEATHER_API'),
                                                             'units': 'metric'},
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
                                                                      'lat': coordinates[0],
                                                                      'lon': coordinates[1],
                                                                      'appid': Variable.get('OPENWEATHER_API'),
                                                                      'dt': '{{ execution_date.timestamp()|int }}',
                                                                      'units': 'metric'
                                                                  }
                                                                  )

        transform_data = PythonOperator(task_id=f'transform_data_{city}',
                                        python_callable=transforming_data,
                                        op_kwargs={'city': f'{city}'},
                                        trigger_rule='none_failed'
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

        create_table >> choose_endpoint >> retrieve_data_group >> transform_data >> inject_data
