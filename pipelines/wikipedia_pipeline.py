import json
import pandas as pd
import time
import logging
from geopy.geocoders import Nominatim

logger = logging.getLogger(__name__)


NO_IMAGE_PLACEHOLDER = "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0a/No-image-available.png/480px-No-image-available.png"


def get_wikipedia_page(url):
    import requests

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        return response.text

    except requests.RequestException as e:
        return None


def get_wikipedia_data(html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find_all("table", {"class": "wikitable sortable sticky-header"})[0]
    table_rows = table.find_all("tr")
    return table_rows


def clean_data(text):
    text = str(text).strip()
    text = text.replace("&nbsp;", "")
    if text.find(" ♦") != -1:
        text = text.split("♦")[0]
    if text.find("[") != -1:
        text = text.split("[")[0]
    if text.find(" (formerly)") != -1:
        text = text.split(" (formerly)")[0]

    return text.replace("\n", "").strip()


def extract_wikipedia_data(**kwargs):

    url = kwargs.get("url")

    html = get_wikipedia_page(url)
    if html is None:
        return []

    rows = get_wikipedia_data(html)
    if not rows:
        return []

    data = []

    for i in range(1, len(rows)):
        try:
            tds = rows[i].find_all("td")
            if len(tds) < 7:
                continue

            values = {
                "rank": i,
                "stadium": clean_data(tds[0].text),
                "capacity": clean_data(tds[1].text).replace(",", "").replace(".", ""),
                "region": clean_data(tds[2].text),
                "country": clean_data(tds[3].text),
                "city": clean_data(tds[4].text),
                "images": (
                    "https://" + tds[5].find("img").get("src").split("//")[1]
                    if tds[5].find("img")
                    else "NO_IMAGE"
                ),
                "home_team": clean_data(tds[6].text),
            }
            data.append(values)
        except Exception as e:
            continue

    if data:
        data_df = pd.DataFrame(data)
        output_path = "/opt/airflow/data/output.csv"
        data_df.to_csv(output_path, index=False)

    json_rows = json.dumps(data)
    kwargs["ti"].xcom_push(key="rows", value=json_rows)

    logger.info(f"Extracted {len(data)} rows from Wikipedia table.")
    return "OK"


def get_lat_long(country, city, remaining_rows=None):
    geolocator = Nominatim(user_agent="my-airflow-project-v1")
    # time.sleep(0.00)  # Espera 1 ms. Ajuste conforme sua necessidade e política.

    query = f"{city}, {country}"
    if remaining_rows is not None:
        logger.info(f"Geocoding: {query} | Remaining rows: {remaining_rows}")
    else:
        logger.info(f"Geocoding: {query}")
    try:
        location = geolocator.geocode(query, timeout=10)
        if location:
            logger.info(
                f"Found: {location.address} (Lat: {location.latitude}, Lon: {location.longitude})"
            )
            return location.latitude, location.longitude
        else:
            logger.warning(f"Could not find location for: {query}")
            return None
    except Exception as e:
        logger.error(f"Error geocoding {query}: {e}")
        return None


def transform_wikipedia_data(**kwargs):

    data = kwargs["ti"].xcom_pull(task_ids="extract_data_from_wikipedia", key="rows")
    data = json.loads(data)

    stadiums_df = pd.DataFrame(data)

    total_rows = len(stadiums_df)
    logger.info(f"Processing {total_rows} stadiums for testing...")

    # Adicionar geocodificação com contador de linhas restantes
    def geocode_with_counter(row):
        remaining = total_rows - row.name
        return get_lat_long(row["country"], row["stadium"], remaining)

    stadiums_df["location"] = stadiums_df.apply(geocode_with_counter, axis=1)

    stadiums_df["images"] = stadiums_df["images"].apply(
        lambda x: x if x not in ["NO_IMAGE", "", None] else NO_IMAGE_PLACEHOLDER
    )

    stadiums_df["capacity"] = stadiums_df["capacity"].astype(int)

    # handle the duplicates
    duplicates = stadiums_df[stadiums_df.duplicated(["location"])]
    duplicate_count = len(duplicates)

    if duplicate_count > 0:
        logger.info(f"Found {duplicate_count} duplicates, re-geocoding with city...")

        def geocode_duplicates_with_counter(row):
            remaining = duplicate_count - (row.name - duplicates.index[0])
            return get_lat_long(row["country"], row["city"], remaining)

        duplicates["location"] = duplicates.apply(
            geocode_duplicates_with_counter, axis=1
        )
    stadiums_df.update(duplicates)

    # push to xcom
    kwargs["ti"].xcom_push(key="rows", value=stadiums_df.to_json())


def write_wikipedia_data(**kwargs):
    from datetime import datetime

    data = kwargs["ti"].xcom_pull(task_ids="transform_wikipedia_data", key="rows")
    data = json.loads(data)

    stadiums_df = pd.DataFrame(data)

    file_name = (
        "stadium_cleaned_"
        + str(datetime.now().date())
        + "_"
        + str(datetime.now().time()).replace(":", "_")
        + ".csv"
    )

    stadiums_df.to_csv("/opt/airflow/data/" + file_name, index=False)
