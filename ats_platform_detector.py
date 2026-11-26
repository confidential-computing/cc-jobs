#!/usr/bin/env python3
"""
ATS Platform Detector for CC-jobs
Analyzes career URLs to detect hidden ATS platforms that can use existing integrations
"""

import yaml
import requests
import time
from urllib.parse import urlparse, urljoin
import re
from typing import Optional, Dict, List, Tuple

def analyze_url_for_ats(url: str, timeout: int = 10) -> Dict[str, any]:
    """Analyze a career URL to detect ATS platform indicators"""
    result = {
        'url': url,
        'domain': '',
        'ats_platform': None,
        'integration_type': None,
        'additional_config': {},
        'confidence': 0,
        'redirect_url': None,
        'error': None,
        'html_indicators': []
    }
    
    try:
        # Parse domain
        parsed = urlparse(url)
        result['domain'] = parsed.netloc.lower()
        
        # Quick domain-based detection
        domain_patterns = {
            'workday': ('myworkdayjobs.com', 'workday'),
            'greenhouse': ('greenhouse.io', 'greenhouse'),
            'lever': ('lever.co', 'lever'),
            'bamboohr': ('bamboohr.com', 'bamboohr'),
            'smartrecruiters': ('smartrecruiters.com', 'smartrecruiters'),
            'jobvite': ('jobvite.com', 'jobvite')
        }
        
        for platform, (pattern, integration) in domain_patterns.items():
            if pattern in result['domain']:
                result['ats_platform'] = platform
                result['integration_type'] = integration
                result['confidence'] = 90
                return result
        
        # Fetch page to analyze content and redirects
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        
        # Check for redirects
        if response.url != url:
            result['redirect_url'] = response.url
            redirect_domain = urlparse(response.url).netloc.lower()
            
            # Re-analyze redirect domain
            for platform, (pattern, integration) in domain_patterns.items():
                if pattern in redirect_domain:
                    result['ats_platform'] = platform
                    result['integration_type'] = integration
                    result['confidence'] = 85
                    return result
        
        # Analyze HTML content for ATS indicators
        html = response.text.lower()
        
        html_patterns = {
            'workday': [
                'myworkdayjobs.com',
                'workday-cdn.com',
                'workday.com',
                'data-automation-id="workday',
                'workday application'
            ],
            'greenhouse': [
                'greenhouse.io',
                'greenhouse-api',
                'data-greenhouse',
                'greenhouse jobs',
                'powered by greenhouse'
            ],
            'lever': [
                'lever.co',
                'lever-api',
                'data-lever',
                'powered by lever'
            ],
            'successfactors': [
                'successfactors',
                'sap successfactors',
                'sf-recruiting',
                'brassring'
            ],
            'oracle_hcm': [
                'oraclecloud.com',
                'oracle hcm',
                'oracle recruiting',
                'fa.oraclecloud.com'
            ],
            'eightfold': [
                'eightfold.ai',
                'eightfold-api',
                'powered by eightfold'
            ],
            'jobvite': [
                'jobvite.com',
                'jobvite-api',
                'powered by jobvite'
            ]
        }
        
        for platform, patterns in html_patterns.items():
            matches = []
            for pattern in patterns:
                if pattern in html:
                    matches.append(pattern)
            
            if matches:
                result['html_indicators'].extend(matches)
                if not result['ats_platform'] or len(matches) > 1:
                    result['ats_platform'] = platform
                    result['confidence'] = max(result['confidence'], len(matches) * 20)
        
        # Determine integration type based on detected platform
        integration_mapping = {
            'workday': 'workday',
            'greenhouse': 'greenhouse', 
            'lever': 'lever',
            'successfactors': 'gated',  # Requires JS/complex handling
            'oracle_hcm': 'gated',      # Requires API research
            'eightfold': 'gated',       # Requires API research
            'jobvite': 'gated'          # Often blocks bots
        }
        
        if result['ats_platform']:
            result['integration_type'] = integration_mapping.get(result['ats_platform'], 'gated')
            
            # Generate additional config for known platforms
            if result['ats_platform'] == 'workday':
                # Extract workday instance details
                workday_patterns = re.findall(r'([a-zA-Z0-9\-]+)\.wd\d+\.myworkdayjobs\.com', 
                                             response.url + ' ' + html)
                if workday_patterns:
                    company_slug = workday_patterns[0]
                    result['additional_config'] = {
                        'workday_api': response.url.replace('/careers', '').replace('/jobs', ''),
                        'workday_job_base': response.url,
                        'slug': company_slug
                    }
            
            elif result['ats_platform'] == 'greenhouse':
                # Extract greenhouse board slug
                gh_patterns = re.findall(r'greenhouse\.io/([a-zA-Z0-9\-]+)', html)
                if gh_patterns:
                    result['additional_config'] = {'slug': gh_patterns[0]}
                    
            elif result['ats_platform'] == 'lever':
                # Extract lever board slug  
                lever_patterns = re.findall(r'jobs\.lever\.co/([a-zA-Z0-9\-]+)', html)
                if lever_patterns:
                    result['additional_config'] = {'slug': lever_patterns[0]}
        
    except requests.RequestException as e:
        result['error'] = str(e)
    except Exception as e:
        result['error'] = f"Analysis error: {str(e)}"
    
    return result

def detect_ats_platforms(members_file: str = 'members.yaml') -> List[Dict]:
    """Detect ATS platforms for gated and no_page members"""
    
    with open(members_file) as f:
        data = yaml.safe_load(f)
    
    # Target members that might have hidden ATS platforms
    target_members = []
    for member in data['members']:
        mtype = member.get('type', 'no_page')
        if mtype in ['gated', 'no_page'] and member.get('careers_url'):
            target_members.append(member)
    
    results = []
    print(f"🔍 Analyzing {len(target_members)} members for ATS platforms...")
    print()
    
    for i, member in enumerate(target_members):
        print(f"[{i+1}/{len(target_members)}] Analyzing {member['name']}...")
        
        analysis = analyze_url_for_ats(member['careers_url'])
        
        # Combine member info with analysis
        result = {
            'member': member,
            'analysis': analysis
        }
        results.append(result)
        
        # Show immediate results
        if analysis['ats_platform']:
            confidence = analysis['confidence']
            platform = analysis['ats_platform']
            integration = analysis['integration_type']
            print(f"  ✅ {platform.upper()} detected (confidence: {confidence}%) -> {integration}")
            if analysis['additional_config']:
                print(f"     Config: {analysis['additional_config']}")
        else:
            print(f"  ❌ No ATS platform detected")
        
        if analysis['error']:
            print(f"  ⚠️  Error: {analysis['error']}")
        
        print()
        
        # Be respectful - small delay between requests
        time.sleep(1)
    
    return results

def generate_integration_updates(results: List[Dict]) -> List[Dict]:
    """Generate member.yaml updates for detected ATS platforms"""
    
    updates = []
    
    for result in results:
        member = result['member']
        analysis = result['analysis']
        
        # Only suggest updates for high-confidence ATS detections
        if (analysis['ats_platform'] and 
            analysis['confidence'] >= 70 and 
            analysis['integration_type'] in ['workday', 'greenhouse', 'lever']):
            
            update = {
                'name': member['name'],
                'current_type': member.get('type', 'no_page'),
                'suggested_type': analysis['integration_type'],
                'confidence': analysis['confidence'],
                'careers_url': member.get('careers_url'),
                'additional_config': analysis['additional_config'],
                'ats_platform': analysis['ats_platform']
            }
            updates.append(update)
    
    return updates

if __name__ == "__main__":
    print("🎯 CC-Jobs ATS Platform Detection")
    print("==================================")
    
    # Run detection
    results = detect_ats_platforms()
    
    # Generate integration suggestions
    updates = generate_integration_updates(results)
    
    print("\n📊 DETECTION SUMMARY")
    print("====================")
    print(f"Members analyzed: {len(results)}")
    print(f"ATS platforms detected: {len([r for r in results if r['analysis']['ats_platform']])}")
    print(f"Ready for integration: {len(updates)}")
    
    if updates:
        print("\n🎯 RECOMMENDED INTEGRATIONS")
        print("===========================")
        for update in updates:
            name = update['name']
            platform = update['ats_platform']
            integration = update['suggested_type']
            confidence = update['confidence']
            config = update['additional_config']
            
            print(f"\n{name}")
            print(f"  Platform: {platform.upper()} ({confidence}% confidence)")
            print(f"  Integration: {update['current_type']} -> {integration}")
            print(f"  URL: {update['careers_url']}")
            if config:
                print(f"  Config: {config}")
    
    print("\n✨ ATS detection complete!")