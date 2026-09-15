import requests
from bs4 import BeautifulSoup
import json
import re
import xml.etree.ElementTree as ET
import os
from urllib.parse import unquote
from urllib.parse import urlparse, urlunparse
from datetime import datetime
import unicodedata
import urllib3

# Disable the InsecureRequestWarning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_proxies(country_code):
    url = f"https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=10000&country={country_code}&ssl=all&anonymity=elite"
    response = requests.get(url)
    if response.status_code == 200:
        proxy_list = response.text.splitlines()
        return [f"socks4://{proxy}" for proxy in proxy_list if proxy.strip()]
    else:
        print(f"Failed to fetch proxies for {country_code}. Status code: {response.status_code}")
        return []

def fetch_channel_list(proxy, retries=3):
    url = "https://tubitv.com/live"
    for attempt in range(retries):
        try:
            if proxy:
                response = requests.get(url, proxies={"http": proxy, "https": proxy}, verify=False, timeout=20)
            else:
                response = requests.get(url, verify=False, timeout=20)
            response.encoding = 'utf-8'
            if response.status_code != 200:
                print(f"Failed to fetch data from {url} using proxy {proxy}. Status code: {response.status_code}")
                continue

            html_content = response.content.decode('utf-8', errors='replace')
            html_content = html_content.replace('', 'ñ')
            soup = BeautifulSoup(html_content, "html.parser")

            script_tags = soup.find_all("script")
            target_script = None
            for script in script_tags:
                if script.string and script.string.strip().startswith("window.__data"):
                    target_script = script.string
                    break

            if not target_script:
                print("Error: Could not locate the JSON-like data in the page.")
                print(f"Logging response content for debugging:\n{html_content[:1000]}...")
                continue

            start_index = target_script.find("{")
            end_index = target_script.rfind("}") + 1
            json_string = target_script[start_index:end_index]
            json_string = json_string.encode('utf-8', errors='replace').decode('utf-8')
            json_string = json_string.replace('undefined', 'null')
            json_string = re.sub(r'new Date\("([^"]*)"\)', r'"\1"', json_string)
            print(f"Extracted JSON-like data (first 500 chars): {json_string[:500]}...")
            data = json.loads(json_string)
            print(f"Successfully decoded JSON data!")
            return data
        except requests.RequestException as e:
            print(f"Error fetching data using proxy {proxy}: {e}")
    return []

def find_keys(node, target_key):
    """Recursively search for all occurrences of a target key in a JSON dict/list."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == target_key:
                yield value
            elif isinstance(value, (dict, list)):
                yield from find_keys(value, target_key)
    elif isinstance(node, list):
        for item in node:
            yield from find_keys(item, target_key)

def create_group_mapping(json_data):
    group_mapping = {}
    for content_ids in find_keys(json_data, 'contentIdsByContainer'):
        for container_list in content_ids.values():
            if isinstance(container_list, list):
                for category in container_list:
                    group_name = category.get('name', 'Other')
                    for content_id in category.get('contents', []):
                        group_mapping[str(content_id)] = group_name
    return group_mapping

def fetch_epg_data(channel_list, proxy=None):
    epg_data = []
    group_size = 150
    grouped_ids = [channel_list[i:i + group_size] for i in range(0, len(channel_list), group_size)]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    for group in grouped_ids:
        url = "https://tubitv.com/oz/epg/programming"
        params = {"content_id": ','.join(map(str, group))}
        proxies = {"http": proxy, "https": proxy} if proxy else None

        try:
            response = requests.get(url, params=params, headers=headers, proxies=proxies, verify=False, timeout=20)
            if response.status_code != 200:
                print(f"Failed to fetch EPG API. Status: {response.status_code}")
                continue

            epg_json = response.json()
            epg_data.extend(epg_json.get('rows', []))
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(f"Error fetching EPG JSON: {e}")

    return epg_data

def clean_stream_url(url):
    parsed_url = urlparse(url)
    clean_url = urlunparse((parsed_url.scheme, parsed_url.netloc, parsed_url.path, '', '', ''))
    return clean_url

def normalize_text(text):
    normalized_text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    return normalized_text

def create_m3u_playlist(epg_data, group_mapping, country):
    sorted_epg_data = sorted(epg_data, key=lambda x: x.get('title', '').lower())
    playlist = f"#EXTM3U url-tvg=\"https://raw.githubusercontent.com/BuddyChewChew/tubi-scraper/refs/heads/main/tubi_epg.xml\"\n"
    playlist += f"# Generated on {datetime.now().isoformat()}\n"  # Add timestamp
    seen_urls = set()

    for elem in sorted_epg_data:
        channel_name = elem.get('title', 'Unknown Channel')
        channel_name = channel_name.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
        stream_url = unquote(elem['video_resources'][0]['manifest']['url']) if elem.get('video_resources') else ''
        clean_url = clean_stream_url(stream_url)
        tvg_id = str(elem.get('content_id', ''))
        logo_url = elem.get('images', {}).get('thumbnail', [None])[0]
        group_title = group_mapping.get(tvg_id, 'Other')
        group_title = group_title.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')

        if clean_url and clean_url not in seen_urls:
            playlist += f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo_url}" group-title="{group_title}",{channel_name}\n{clean_url}\n'
            seen_urls.add(clean_url)

    return playlist

def convert_to_xmltv_format(iso_time):
    try:
        dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%SZ")
        xmltv_time = dt.strftime("%Y%m%d%H%M%S +0000")
        return xmltv_time
    except ValueError:
        return iso_time

def create_epg_xml(epg_data):
    root = ET.Element("tv")
    for station in epg_data:
        channel = ET.SubElement(root, "channel", id=str(station.get("content_id")))
        display_name = ET.SubElement(channel, "display-name")
        display_name.text = station.get("title", "Unknown Title")
        icon = ET.SubElement(channel, "icon", src=station.get("images", {}).get("thumbnail", [None])[0])

        for program in station.get('programs', []):
            programme = ET.SubElement(root, "programme", channel=str(station.get("content_id")))
            start_time = convert_to_xmltv_format(program.get("start_time", ""))
            stop_time = convert_to_xmltv_format(program.get("end_time", ""))
            programme.set("start", start_time)
            programme.set("stop", stop_time)
            title = ET.SubElement(programme, "title")
            title.text = program.get("title", "")
            if program.get("description"):
                desc = ET.SubElement(programme, "desc")
                desc.text = program.get("description", "")

    tree = ET.ElementTree(root)
    return tree

def save_file(content, filename):
    file_path = os.path.join(os.getcwd(), filename)  # Use current working directory
    with open(file_path, 'w', encoding='utf-8') as file:
        file.write(content)
    print(f"File saved: {file_path}")

def save_epg_to_file(tree, filename):
    file_path = os.path.join(os.getcwd(), filename)  # Use current working directory
    tree.write(file_path, encoding='utf-8', xml_declaration=True)
    print(f"EPG XML file saved: {file_path}")

def main():
    countries = ["US"]
    for country in countries:
        proxies = get_proxies(country)
        working_proxy = None
        json_data = None
        
        if not proxies:
            print(f"No proxies found for country {country}. Trying without proxy...")
            json_data = fetch_channel_list(None)
        else:
            for proxy in proxies:
                print(f"Trying proxy {proxy} for country {country}...")
                json_data = fetch_channel_list(proxy)
                if json_data:
                    working_proxy = proxy
                    break
            else:
                print(f"All proxies failed for {country}. Trying without proxy...")
                json_data = fetch_channel_list(None)

        if not json_data:
            print(f"Failed to fetch data for {country}")
            continue

        print(f"Successfully fetched data for country {country}")
        
        # Robustly extract channel IDs using the recursive search
        channel_list = []
        for content_ids in find_keys(json_data, 'contentIdsByContainer'):
            for container_list in content_ids.values():
                if isinstance(container_list, list):
                    for category in container_list:
                        channels = category.get('contents', [])
                        if channels:
                            channel_list.extend(channels)
                            
        # Remove any duplicate IDs
        channel_list = list(set(channel_list))
        
        if not channel_list:
            print("No channel IDs found in the JSON. The site structure may have changed.")
            continue

        # Pass the successful proxy into the EPG fetcher
        epg_data = fetch_epg_data(channel_list, proxy=working_proxy)
        
        if not epg_data:
            print("No EPG data found.")
            continue

        group_mapping = create_group_mapping(json_data)
        m3u_playlist = create_m3u_playlist(epg_data, group_mapping, country.lower())
        epg_tree = create_epg_xml(epg_data)

        save_file(m3u_playlist, "tubi_playlist.m3u")
        save_epg_to_file(epg_tree, "tubi_epg.xml")

if __name__ == "__main__":
    main()
    
