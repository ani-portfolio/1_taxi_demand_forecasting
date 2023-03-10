from pathlib import Path
from typing import List, Optional
from tqdm import tqdm
from IPython.display import display
import warnings
import requests
import pandas as pd
import numpy as np
from src.paths import RAW_DATA_DIR, TRANSFORMED_DATA_DIR, PARENT_DIR

def download_raw_data_one_file(year: int, month: int) -> Path:
    """Downloads the raw parquet data file for the given year and month.
    The parquet file contains historical data for yellow taxi rides in New York City.

    Args:
        year (int): The year of the data file.
        month (int): The month of the data file.

    Returns:
        Path: The path to the downloaded file.
    """

    URL = f'https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_{year}-{month:02d}.parquet'
    response = requests.get(URL)

    if response.status_code == 200:
        path = RAW_DATA_DIR / f'rides_{year}-{month:02d}.parquet'
        open(path, "wb").write(response.content)
        return path
    else:
        raise Exception(f'{URL} is not available')


def validate_raw_data(rides: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    '''Removes rows with pickup_datetime outside of the given year and month'''

    rides['pickup_datetime'] = pd.to_datetime(rides['pickup_datetime'])
    rides = rides[(rides['pickup_datetime'].dt.year == year) & (rides['pickup_datetime'].dt.month == month)]

    return rides


def load_raw_data(year:int, months: Optional[List[int]] = None) -> pd.DataFrame:
    '''Loads the raw data for the given year and months'''

    if months is None:
        # download all months
        months = list(range(1, 13))
    else:
        months = [months]

    rides = pd.DataFrame()
    for month in months:
        # check if file exists
        local_file = RAW_DATA_DIR / f'rides_{year}-{month:02d}.parquet'
        if not local_file.exists():
            try:
                download_raw_data_one_file(year, month)
                print(f'Downloaded file for {year}-{month:02d}')
            except:
                print(f'File for {year}-{month:02d} not available ')
                continue
        else:
            print(f'File for {year}-{month:02d} already exists')

        # load data
        rides_one_month = pd.read_parquet(local_file)

        # rename columns
        rides_one_month = rides_one_month[['tpep_pickup_datetime', 'PULocationID']]
        rides_one_month.rename(columns={'tpep_pickup_datetime': 'pickup_datetime', 'PULocationID': 'pickup_location_id'}, inplace=True)

        # validate data
        rides_one_month = validate_raw_data(rides_one_month, year, month)

        # concat to existing data
        rides = pd.concat([rides, rides_one_month], axis=0)

    return rides


def add_missing_dates(agg_rides: pd.DataFrame) -> pd.DataFrame:
    '''Takes an aggregated rides dataframe and adds
    missing dates to the dataframe with a frequency of hourly.
    The function returns the dataframe with the added missing dates.
    '''

    
    location_ids = agg_rides['pickup_location_id'].unique()
    full_range = pd.date_range(agg_rides['pickup_hour'].min(), agg_rides['pickup_hour'].max(), freq='H')

    output = pd.DataFrame()

    for location_id in tqdm(location_ids):

        agg_rides_i = agg_rides[agg_rides['pickup_location_id'] == location_id][['pickup_hour', 'rides']]

        agg_rides_i.set_index('pickup_hour', inplace=True)
        agg_rides_i.index = pd.DatetimeIndex(agg_rides_i.index)
        agg_rides_i = agg_rides_i.reindex(full_range, fill_value=0)

        agg_rides_i['pickup_location_id'] = location_id

        output = pd.concat([output, agg_rides_i])

    output = output.reset_index().rename(columns={'index': 'pickup_hour'})

    return output


def transform_raw_data_into_ts_data(rides: pd.DataFrame) -> pd.DataFrame:
    
    rides['pickup_hour'] = rides['pickup_datetime'].dt.floor('H')
    agg_rides = rides.groupby(['pickup_hour', 'pickup_location_id']).size().reset_index()
    agg_rides.rename(columns={0: 'rides'}, inplace=True)

    agg_rides_all_dates = add_missing_dates(agg_rides)

    return agg_rides_all_dates


def get_indices(ts_data: pd.DataFrame, n_features: int, step_size: int) -> list:
    """
    Return a list of tuples with indices to slice the dataframe.
    """

    # get indices for each lag feature
    subseq_first_idx = 0
    subseq_mid_idx = n_features
    subseq_last_idx = n_features + 1

    indices = []
    stop_position = len(ts_data) - 1
    while subseq_last_idx <= stop_position:
        indices.append((subseq_first_idx, subseq_mid_idx, subseq_last_idx))
        subseq_first_idx += step_size
        subseq_mid_idx += step_size
        subseq_last_idx += step_size
    
    return indices


def create_ts_dataset(ts_data: pd.DataFrame, n_features: int, step_size: int) -> List[int]:
    """
    Get cutoff indices to split dataframe rows into features and target.
    """
    
    # ensure each location has the same number of rows
    assert len(ts_data.groupby(['pickup_location_id']).count().value_counts()) == 1, 'Each location ID does not have the same number of rows.'

    # ensure each location and pickup_date has same number of rows (24 rows for each ID and Date)
    assert len(ts_data.groupby(['pickup_location_id', ts_data['pickup_hour'].dt.date]).count().reset_index(drop=True).value_counts()) == 1, 'Each location ID & Date does not have the same number of rows.'
    

    # get features and target using indices
    location_ids = ts_data['pickup_location_id'].unique()
    features = pd.DataFrame()
    targets = pd.DataFrame()
    
    for location_id in tqdm(location_ids):
        ts_data_i = ts_data[ts_data['pickup_location_id'] == location_id].sort_values(by=['pickup_hour'])

        # get indices for each lag feature
        indices = get_indices(ts_data_i, n_features, step_size)
        
        # slice and transpose data into numpy arrays for features and targets
        n_samples = len(indices)
        x = np.ndarray(shape=(n_samples, n_features), dtype=np.float32)
        y = np.ndarray(shape=(n_samples), dtype=np.float32)

        pickup_hours = []
        for i, idx in enumerate(indices):
            x[i, :] = ts_data_i.iloc[idx[0]:idx[1]]['rides'].values
            y[i] = ts_data_i.iloc[idx[1]:idx[2]]['rides'].values
            pickup_hours.append(ts_data_i.iloc[idx[1]]['pickup_hour'])

        # convert x numpy arrays to pandas dataframe
        features_one_location = pd.DataFrame(x, columns=[f'rides_previous_{i+1}_hour' for i in reversed(range(n_features))])
        features_one_location['pickup_hour'] = pickup_hours
        features_one_location['pickup_location_id'] = location_id

        # convert y numpy arrays to pandas dataframe
        targets_one_location = pd.DataFrame(y, columns=[f'target_rides_next_hour'])

        # concatenate results
        features = pd.concat([features, features_one_location])
        targets = pd.concat([targets, targets_one_location])

    features.reset_index(inplace=True, drop=True)
    targets.reset_index(inplace=True, drop=True)

    return features, targets['target_rides_next_hour']


def download_and_load_nyc_taxi_zone_data():
    '''Downloads and loads the NYC taxi zone data'''

    # check if file exists
    local_file = RAW_DATA_DIR / f'nyc_zone_data.csv'
    if not local_file.exists():
        URL = f'https://data.cityofnewyork.us/api/views/755u-8jsi/rows.csv?accessType=DOWNLOAD'
        response = requests.get(URL)

        if response.status_code == 200:
            path = RAW_DATA_DIR / f'nyc_zone_data.csv'
            open(path, "wb").write(response.content)
        else:
            raise Exception(f'{URL} is not available')
    else:
        path = RAW_DATA_DIR / f'nyc_zone_data.csv'
    
    # load data
    nyc_zone_data = pd.read_csv(path)
    nyc_zone_data.drop_duplicates('LocationID', inplace=True)

    return nyc_zone_data