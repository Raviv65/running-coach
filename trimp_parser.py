import json, math
from datetime import datetime

# Defaults — overridden by athlete profile (meta.athlete.hr_max / hr_rest)
HR_MAX_DEFAULT = 160
HR_REST_DEFAULT = 54

def parse_time(t):
    for tz in ['+03:00', '+02:00', '+01:00', '+00:00']:
        t = t.replace(tz, tz.replace(':', ''))
    return datetime.strptime(t, '%Y-%m-%dT%H:%M:%S.%f%z')

def compute_trimp_from_file(path: str, hr_max: int = HR_MAX_DEFAULT, hr_rest: int = HR_REST_DEFAULT) -> dict:
    with open(path) as f:
        data = json.load(f)
    return compute_trimp_from_data(data, hr_max=hr_max, hr_rest=hr_rest)

def compute_trimp_from_data(data: dict, hr_max: int = HR_MAX_DEFAULT, hr_rest: int = HR_REST_DEFAULT) -> dict:
    header = data['DeviceLog']['Header']
    samples = data['DeviceLog']['Samples']

    hr_samples = [
        {'time': s['TimeISO8601'], 'hr_bpm': s['HR'] * 60}
        for s in samples if 'HR' in s and 'TimeISO8601' in s
    ]

    trimp = 0.0
    hr_timeseries = []
    for i, s in enumerate(hr_samples):
        hr = s['hr_bpm']
        if i < len(hr_samples) - 1:
            dt_min = (parse_time(hr_samples[i+1]['time']) - parse_time(s['time'])).total_seconds() / 60
        else:
            dt_min = 1/60
        if dt_min > 0.1:
            dt_min = 1/60
        hr_timeseries.append({'t': s['time'], 'hr': round(hr, 1)})
        if hr >= hr_rest:
            hrr = max(0, min(1, (hr - hr_rest) / (hr_max - hr_rest)))
            # Male Banister TRIMP coefficients (0.86, 1.67)
            trimp += dt_min * hrr * 0.86 * math.exp(1.67 * hrr)

    zones = header.get('HrZones', {})
    act_date = header['DateTime'][:10]

    # Sample HR timeseries to max 200 points for storage
    step = max(1, len(hr_timeseries) // 200)
    hr_sampled = hr_timeseries[::step]

    # Calories: Energy field is in Joules; divide by 4184 to get kcal
    energy_j = header.get('Energy')
    calories_kcal = round(energy_j / 4184) if energy_j is not None else None

    # TSS approximation from EPOC: (EPOC / 200) * 100
    epoc = header.get('EPOC')
    tss = round((epoc / 200) * 100) if epoc is not None else None

    # Convert hr_timeseries timestamps to seconds-from-start
    if hr_timeseries:
        t0 = parse_time(hr_timeseries[0]['t'])
        hr_sampled_sec = [
            {'t': round((parse_time(p['t']) - t0).total_seconds()), 'hr': p['hr']}
            for p in hr_sampled
        ]
    else:
        hr_sampled_sec = hr_sampled

    return {
        'date': act_date,
        'trimp': round(trimp, 1),
        'duration_min': round(header['Duration'] / 60, 1),
        'distance_km': round(header['Distance'] / 1000, 2),
        'epoc': epoc,
        'peak_training_effect': header.get('PeakTrainingEffect'),
        'recovery_time_hrs': round(header.get('RecoveryTime', 0) / 3600, 1),
        'step_count': header.get('StepCount'),
        'calories_kcal': calories_kcal,
        'tss': tss,
        'avg_hr': round(sum(s['hr_bpm'] for s in hr_samples) / len(hr_samples), 1) if hr_samples else None,
        'max_hr': round(max(s['hr_bpm'] for s in hr_samples), 1) if hr_samples else None,
        'hr_zones': {
            'z1': round(zones.get('Zone1Duration', 0) / 60, 1),
            'z2': round(zones.get('Zone2Duration', 0) / 60, 1),
            'z3': round(zones.get('Zone3Duration', 0) / 60, 1),
            'z4': round(zones.get('Zone4Duration', 0) / 60, 1),
            'z5': round(zones.get('Zone5Duration', 0) / 60, 1),
        },
        'hr_timeseries': hr_sampled_sec,
        'title': 'Running',
        'sport': 'running',
        'source': 'suunto_json',
    }
