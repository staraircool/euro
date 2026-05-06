"""EuroPages Company Scraper - Lightweight version.

Scrapes company data from europages.co.uk including:
- Company name
- Email address
- Website URL
- Phone number
- Country, address, description, and company type

Uses httpx + BeautifulSoup for reliable scraping without heavy framework dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('europages-scraper')

# Base URL for EuroPages
BASE_URL = 'https://www.europages.co.uk'

# Common headers to mimic a browser
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}


async def main() -> None:
    """Main entry point for the Apify Actor."""
    # Use Apify REST API directly to avoid SDK version conflicts
    apify_token = os.environ.get('APIFY_TOKEN', '')
    dataset_id = os.environ.get('APIFY_DEFAULT_DATASET_ID', '')
    kv_store_id = os.environ.get('APIFY_DEFAULT_KEY_VALUE_STORE_ID', '')

    # Read input from key-value store or local file
    actor_input = {}
    if apify_token and kv_store_id:
        log.info('Running on Apify platform')
        async with httpx.AsyncClient() as api_client:
            try:
                resp = await api_client.get(
                    f'https://api.apify.com/v2/key-value-stores/{kv_store_id}/records/INPUT',
                    params={'token': apify_token},
                )
                if resp.status_code == 200:
                    actor_input = resp.json()
            except Exception as e:
                log.warning(f'Failed to read input: {e}')
    else:
        # Local mode - read from storage
        input_path = os.path.join('storage', 'key_value_stores', 'default', 'INPUT.json')
        if os.path.exists(input_path):
            with open(input_path) as f:
                actor_input = json.load(f)

    log.info(f'Input: {json.dumps(actor_input, indent=2)}')

    # Run the scraper
    results = await run_scraper(actor_input)

    # Push results to dataset
    if apify_token and dataset_id:
        async with httpx.AsyncClient() as api_client:
            for item in results:
                try:
                    await api_client.post(
                        f'https://api.apify.com/v2/datasets/{dataset_id}/items',
                        params={'token': apify_token},
                        json=[item],
                    )
                except Exception as e:
                    log.error(f'Failed to push data: {e}')
    else:
        # Save locally
        dataset_dir = os.path.join('storage', 'datasets', 'default')
        os.makedirs(dataset_dir, exist_ok=True)
        for i, item in enumerate(results):
            with open(os.path.join(dataset_dir, f'{i:06d}.json'), 'w') as f:
                json.dump(item, f, indent=2)

    log.info(f'Scraping complete! Pushed {len(results)} companies to dataset.')


async def run_scraper(actor_input: dict) -> list[dict]:
    """Run the EuroPages scraper with the given input."""
    search_query = actor_input.get('searchQuery', 'construction')
    start_urls = actor_input.get('startUrls', [])
    max_results = actor_input.get('maxResults', 100)
    max_pages = actor_input.get('maxPages', 5)

    results = []

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        if start_urls:
            # Use user-provided URLs
            urls_to_process = []
            for url_item in start_urls:
                url = url_item.get('url', url_item) if isinstance(url_item, dict) else str(url_item)
                urls_to_process.append(url)
        else:
            # Build search URL from query
            urls_to_process = [f'{BASE_URL}/companies/{quote_plus(search_query)}.html']

        # Collect company detail URLs from listing pages
        company_urls = []
        for listing_url in urls_to_process:
            if _is_company_url(listing_url):
                company_urls.append(listing_url)
            else:
                found = await scrape_listing_pages(client, listing_url, max_pages, max_results)
                company_urls.extend(found)
                if len(company_urls) >= max_results > 0:
                    company_urls = company_urls[:max_results]
                    break

        log.info(f'Found {len(company_urls)} company URLs to scrape')

        # Scrape each company detail page
        for i, url in enumerate(company_urls):
            if max_results > 0 and len(results) >= max_results:
                break

            try:
                log.info(f'[{i+1}/{len(company_urls)}] Scraping: {url}')
                company_data = await scrape_company_page(client, url)
                if company_data and company_data.get('companyName'):
                    results.append(company_data)
                    log.info(
                        f'  -> {company_data["companyName"]} | '
                        f'Phone: {company_data.get("phoneNumber", "N/A")} | '
                        f'Email: {company_data.get("email", "N/A")}'
                    )
                else:
                    log.warning(f'  -> No data extracted from {url}')
            except Exception as e:
                log.error(f'  -> Error scraping {url}: {e}')

            # Small delay between requests
            await asyncio.sleep(1)

    return results


async def scrape_listing_pages(
    client: httpx.AsyncClient,
    base_url: str,
    max_pages: int,
    max_results: int,
) -> list[str]:
    """Scrape listing/search pages to find company URLs."""
    company_urls = []

    for page_num in range(1, max_pages + 1):
        if max_results > 0 and len(company_urls) >= max_results:
            break

        # Build the page URL
        if page_num == 1:
            url = base_url
        elif '?' in base_url:
            url = f'{base_url}&page={page_num}'
        else:
            url = f'{base_url}?page={page_num}'

        log.info(f'Fetching listing page {page_num}: {url}')

        try:
            response = await client.get(url)
            response.raise_for_status()
        except Exception as e:
            log.error(f'Failed to fetch listing page {page_num}: {e}')
            break

        soup = BeautifulSoup(response.text, 'html.parser')

        # Find company links - EuroPages pattern: /COMPANY-NAME/SEACxxxxxx-xxx.html
        links_found = 0
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if re.search(r'/[A-Za-z0-9][^/]*/SEAC\d+-\d+\.html', href):
                full_url = href if href.startswith('http') else urljoin(BASE_URL, href)
                if full_url not in company_urls:
                    company_urls.append(full_url)
                    links_found += 1

        log.info(f'  Found {links_found} company links on page {page_num}')

        if links_found == 0:
            log.info('  No more company links found, stopping pagination')
            break

        # Small delay between pages
        await asyncio.sleep(1)

    return company_urls


async def scrape_company_page(client: httpx.AsyncClient, url: str) -> dict:
    """Scrape a single company detail page."""
    try:
        response = await client.get(url)
        response.raise_for_status()
    except Exception as e:
        log.error(f'Failed to fetch company page: {e}')
        return {}

    html = response.text
    soup = BeautifulSoup(html, 'html.parser')

    data = {
        'companyName': '',
        'email': '',
        'website': '',
        'phoneNumber': '',
        'country': '',
        'address': '',
        'description': '',
        'companyType': '',
        'europagesUrl': url,
    }

    # --- COMPANY NAME ---
    h1 = soup.find('h1')
    if h1:
        # Get text, removing badge elements
        for badge in h1.find_all(['span', 'div'], class_=lambda c: c and ('badge' in c.lower() or 'verified' in c.lower())):
            badge.decompose()
        name = h1.get_text(strip=True)
        name = re.sub(r'\s*Verified\s*', '', name).strip()
        data['companyName'] = name

    # Domains to exclude from email matching (tracking, analytics, etc.)
    excluded_email_domains = [
        'europages', 'example.com', 'sentry.io', 'sentry-next',
        'googleapis', 'google.com', 'facebook.com', 'hotjar.com',
        'segment.io', 'mixpanel.com', 'intercom.io',
    ]

    # --- EMAIL ---
    # Look for mailto: links
    mailto_links = soup.find_all('a', href=re.compile(r'^mailto:', re.IGNORECASE))
    for link in mailto_links:
        email = link['href'].replace('mailto:', '').split('?')[0].strip()
        if email and '@' in email and not any(d in email.lower() for d in excluded_email_domains):
            data['email'] = email
            break

    # Fallback: search entire page text for email patterns
    if not data['email']:
        email_matches = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', html)
        for email in email_matches:
            if not any(d in email.lower() for d in excluded_email_domains):
                data['email'] = email
                break

    # --- WEBSITE ---
    # Look for external links (not to europages, social media, etc.)
    excluded_domains = ['europages', 'google', 'facebook', 'linkedin', 'twitter', 'instagram', 'youtube']
    for a_tag in soup.find_all('a', href=True, target='_blank'):
        href = a_tag['href']
        if href.startswith(('http://', 'https://')) and not any(d in href for d in excluded_domains):
            data['website'] = href
            break

    # Fallback: Look for links with "website" in text
    if not data['website']:
        for a_tag in soup.find_all('a', href=True):
            text = a_tag.get_text(strip=True).lower()
            href = a_tag['href']
            if ('website' in text or 'web site' in text or 'visit' in text) and \
               href.startswith(('http://', 'https://')) and \
               not any(d in href for d in excluded_domains):
                data['website'] = href
                break

    # --- PHONE NUMBER ---
    # Look for tel: links
    tel_links = soup.find_all('a', href=re.compile(r'^tel:', re.IGNORECASE))
    for link in tel_links:
        phone = link['href'].replace('tel:', '').strip()
        if phone:
            data['phoneNumber'] = phone
            break

    # Fallback: look for phone patterns in page
    if not data['phoneNumber']:
        phone_match = re.search(
            r'(?:Phone|Tel|Telephone|Fax)[:\s]*([+\d\s().-]{7,20})',
            soup.get_text(), re.IGNORECASE
        )
        if phone_match:
            data['phoneNumber'] = phone_match.group(1).strip()

    # --- COUNTRY ---
    country_el = soup.find(class_=re.compile(r'country', re.IGNORECASE))
    if country_el:
        data['country'] = country_el.get_text(strip=True)

    # Fallback: look for known countries in header area
    if not data['country'] and h1:
        parent = h1.parent
        if parent:
            parent_text = parent.get_text()
            countries = [
                'Germany', 'France', 'Italy', 'Spain', 'Poland', 'Netherlands',
                'Belgium', 'Austria', 'Switzerland', 'United Kingdom', 'Portugal',
                'Czech Republic', 'Czechia', 'Romania', 'Sweden', 'Denmark',
                'Finland', 'Norway', 'Ireland', 'Hungary', 'Greece', 'Turkey',
                'Croatia', 'Bulgaria', 'Slovakia', 'Slovenia', 'Lithuania',
                'Latvia', 'Estonia', 'Luxembourg', 'Malta', 'Cyprus',
            ]
            for country in countries:
                if country in parent_text:
                    data['country'] = country
                    break

    # --- ADDRESS ---
    addr_el = soup.find(class_=re.compile(r'address', re.IGNORECASE))
    if addr_el:
        data['address'] = re.sub(r'\s+', ' ', addr_el.get_text(strip=True))
    if not data['address']:
        addr_tag = soup.find('address')
        if addr_tag:
            data['address'] = re.sub(r'\s+', ' ', addr_tag.get_text(strip=True))

    # --- DESCRIPTION ---
    desc_el = soup.find(class_=re.compile(r'description', re.IGNORECASE))
    if desc_el:
        data['description'] = desc_el.get_text(strip=True)[:500]
    if not data['description']:
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc and meta_desc.get('content'):
            data['description'] = meta_desc['content'][:500]

    # --- COMPANY TYPE ---
    type_keywords = [
        'Manufacturer', 'Distributor', 'Service provider',
        'Wholesaler', 'Retailer', 'Subcontractor', 'Agent',
    ]
    page_text = soup.get_text()
    for keyword in type_keywords:
        if keyword in page_text:
            data['companyType'] = keyword
            break

    # Clean up the data
    return _clean_company_data(data)


def _is_company_url(url: str) -> bool:
    """Check if a URL is a EuroPages company detail page."""
    return bool(re.search(r'/[A-Za-z0-9][^/]*/SEAC\d+-\d+\.html', url))


def _clean_company_data(data: dict) -> dict:
    """Clean and validate scraped company data."""
    cleaned = {}
    for key, value in data.items():
        if isinstance(value, str):
            value = value.strip()
            value = re.sub(r'\s+', ' ', value)

            if key == 'companyName':
                value = re.sub(r'\s*Verified\s*', '', value).strip()
                value = re.sub(r'\s*✓\s*', '', value).strip()

            if key == 'phoneNumber':
                value = re.sub(r'(?i)show\s*(phone|number|tel)', '', value).strip()

            if key == 'email':
                if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', value):
                    value = ''

            if key == 'website':
                if not value.startswith(('http://', 'https://')):
                    if value and '.' in value:
                        value = f'https://{value}'
                    else:
                        value = ''

        cleaned[key] = value if value else ''
    return cleaned
