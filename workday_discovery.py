#!/usr/bin/env python3
"""
Workday Instance Discovery for CC-jobs
Tests for hidden Workday instances using common patterns
"""

import yaml
import requests
import time
from urllib.parse import urlparse
import re

def generate_workday_candidates(member_name: str) -> list:
    """Generate potential Workday URLs for a company"""
    
    # Clean company name to likely workday slug
    name = member_name.lower()
    
    # Remove common suffixes
    name = re.sub(r'\s+(inc\.?|llc\.?|ltd\.?|corp\.?|corporation|company|co\.?)$', '', name)
    name = re.sub(r'\s+(technologies?|systems?|solutions?)$', '', name)
    
    # Replace spaces and special chars
    name = re.sub(r'[^a-z0-9]', '', name)
    
    # Common variations
    candidates = [name]
    
    # Add variations
    if len(name) > 10:
        # Try abbreviated versions
        words = re.findall(r'[a-z]+', member_name.lower())
        if len(words) > 1:
            # First word + first letters of others
            abbrev = words[0] + ''.join(w[0] for w in words[1:] if w)
            candidates.append(abbrev)
            
            # Just first word
            candidates.append(words[0])
    
    # Common corporate name mappings
    mapping = {
        'microsoft': ['microsoft', 'msft'],
        'intel': ['intel', 'intelcorp'], 
        'google': ['google', 'alphabet'],
        'meta': ['meta', 'facebook'],
        'jpmorgan': ['jpmc', 'jpmchase', 'jpmorgan'],
        'arm': ['arm', 'armholdings'],
        'qualcomm': ['qualcomm', 'qcom'],
        'ampere': ['ampere', 'amperecomputing'],
        'fujitsu': ['fujitsu', 'fujitsuamerica']
    }
    
    for key, variations in mapping.items():
        if key in name:
            candidates.extend(variations)
    
    # Remove duplicates while preserving order
    unique_candidates = []
    for candidate in candidates:
        if candidate and candidate not in unique_candidates:
            unique_candidates.append(candidate)
    
    return unique_candidates

def test_workday_instance(company_slug: str, workday_domains: list = None) -> dict:
    """Test if a company has a workday instance"""
    
    if workday_domains is None:
        # Common workday domain patterns based on existing members
        workday_domains = ['wd1', 'wd3', 'wd5', 'wd6', 'wd8']
    
    result = {
        'company_slug': company_slug,
        'found': False,
        'workday_api': None,
        'workday_job_base': None,
        'jobs_board': None,
        'test_url': None,
        'error': None
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    for wd_domain in workday_domains:
        # Test pattern: https://company.wd5.myworkdayjobs.com/
        test_base = f'https://{company_slug}.{wd_domain}.myworkdayjobs.com'
        
        try:
            # First test the base domain
            response = requests.get(test_base, headers=headers, timeout=10, allow_redirects=True)
            
            if response.status_code == 200:
                # Look for job board paths in the response
                html = response.text
                
                # Extract job board paths from HTML
                job_board_patterns = re.findall(r'/en-US/([A-Za-z0-9_-]+)', html)
                job_board_patterns.extend(re.findall(r'href=\"/([A-Za-z0-9_-]+)\"', html))
                
                if job_board_patterns:
                    # Use the first job board found
                    job_board = job_board_patterns[0]
                    
                    # Construct the full URLs
                    workday_job_base = f'{test_base}/en-US/{job_board}'
                    workday_api = f'{test_base}/wday/cxs/{company_slug}/{job_board}/jobs'
                    
                    # Test if job board is accessible
                    try:
                        job_response = requests.get(workday_job_base, headers=headers, timeout=10)
                        if job_response.status_code == 200:
                            result.update({
                                'found': True,
                                'workday_api': workday_api,
                                'workday_job_base': workday_job_base,
                                'jobs_board': job_board,
                                'test_url': test_base
                            })
                            return result
                    except:
                        pass
                        
                # Try common job board names if patterns not found
                common_boards = [
                    f'{company_slug}careers',
                    f'{company_slug}jobs', 
                    'careers',
                    'jobs',
                    'external',
                    f'{company_slug}externalcareers'
                ]
                
                for board in common_boards:
                    try:
                        job_url = f'{test_base}/en-US/{board}'
                        job_response = requests.get(job_url, headers=headers, timeout=5)
                        if job_response.status_code == 200:
                            result.update({
                                'found': True,
                                'workday_api': f'{test_base}/wday/cxs/{company_slug}/{board}/jobs',
                                'workday_job_base': job_url,
                                'jobs_board': board,
                                'test_url': test_base
                            })
                            return result
                    except:
                        continue
                        
        except requests.RequestException as e:
            result['error'] = str(e)
            continue
        
        # Small delay between attempts
        time.sleep(0.5)
    
    return result

def discover_workday_instances() -> list:
    """Discover new Workday instances for gated members"""
    
    with open('members.yaml') as f:
        data = yaml.safe_load(f)
    
    # Target Premier and high-value General members that might use Workday
    target_members = []
    for member in data['members']:
        mtype = member.get('type', '')
        tier = member.get('tier', '')
        
        # Focus on gated members, especially Premier and General tier
        if mtype in ['gated', 'no_page'] and tier in ['Premier', 'General']:
            target_members.append(member)
    
    discoveries = []
    
    print(f'🔍 Testing {len(target_members)} high-value members for Workday instances...')
    print()
    
    for i, member in enumerate(target_members):
        name = member['name']
        tier = member['tier']
        
        print(f'[{i+1}/{len(target_members)}] Testing {name} ({tier})...')
        
        # Generate candidate slugs
        candidates = generate_workday_candidates(name)
        print(f'  Candidates: {candidates[:3]}...' if len(candidates) > 3 else f'  Candidates: {candidates}')
        
        found = False
        for candidate in candidates:
            if len(candidate) < 3:  # Skip very short candidates
                continue
                
            result = test_workday_instance(candidate)
            
            if result['found']:
                print(f'  ✅ FOUND: {candidate} -> {result["workday_job_base"]}')
                
                discovery = {
                    'member': member,
                    'workday_result': result
                }
                discoveries.append(discovery)
                found = True
                break
            else:
                print(f'  ❌ {candidate}')
        
        if not found:
            print(f'  ⚠️  No Workday instance found')
        
        print()
        
        # Be respectful with delays
        time.sleep(2)
    
    return discoveries

if __name__ == '__main__':
    print('🎯 Workday Instance Discovery')
    print('=============================')
    
    discoveries = discover_workday_instances()
    
    print('\n📊 DISCOVERY SUMMARY')
    print('====================')
    print(f'Members tested: varies')
    print(f'Workday instances found: {len(discoveries)}')
    
    if discoveries:
        print('\n🎯 WORKDAY INTEGRATIONS READY')
        print('==============================')
        
        for discovery in discoveries:
            member = discovery['member']
            result = discovery['workday_result']
            
            print(f'\n{member["name"]} ({member["tier"]})')
            print(f'  Current: {member.get("type", "no_page")}')
            print('  New: workday')
            print(f'  API: {result["workday_api"]}')
            print(f'  Jobs: {result["workday_job_base"]}')
            print(f'  Board: {result["jobs_board"]}')
    
    print('\n✨ Workday discovery complete!')