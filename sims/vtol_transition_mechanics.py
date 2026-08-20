"""
VTOL Transition Mechanics - Vertical to Forward Flight Transition
Analyzes the mechanics and safety of transitioning from vertical to horizontal flight.
"""

import math

META = {
    "name": "vtol_transition_mechanics",
    "description": "Transition mechanics analysis for VTOL aircraft",
    "why": "Need to understand if and how VTOL transition can work safely"
}

def run(params):
    """
    params: dict with keys:
    - aircraft_weight_N: total aircraft weight (N)
    - vertical_thrust_N: maximum vertical thrust available (N)
    - max_transition_angle_deg: maximum angle from vertical during transition (deg)
    - transition_time_s: time available for transition (s)
    - wind_speed_ms: wind speed during transition (m/s)
    """
    
    weight = params.get("aircraft_weight_N", 1000)
    vertical_thrust = params.get("vertical_thrust_N", 1200)
    max_transition_angle = params.get("max_transition_angle_deg", 45)
    transition_time = params.get("transition_time_s", 10)
    wind_speed = params.get("wind_speed_ms", 0)
    
    # Check if vertical thrust is sufficient
    can_hover = vertical_thrust >= weight
    hover_margin = (vertical_thrust - weight) / weight if weight > 0 else 0
    
    # During transition, vertical component of thrust must support weight
    # Thrust_vertical = T * cos(θ) where θ is angle from vertical
    # At maximum transition angle, check if still supported
    max_angle_rad = math.radians(max_transition_angle)
    thrust_vertical_at_max = vertical_thrust * math.cos(max_angle_rad)
    can_sustain_at_max_angle = thrust_vertical_at_max >= weight
    
    # If thrust insufficient at max angle, find maximum safe angle
    if not can_sustain_at_max_angle:
        # cos(θ_safe) = weight/thrust
        # θ_safe = arccos(weight/thrust)
        if vertical_thrust > weight:
            safe_angle_rad = math.acos(weight / vertical_thrust)
            safe_angle_deg = math.degrees(safe_angle_rad)
        else:
            safe_angle_deg = 0  # Cannot transition at all
    else:
        safe_angle_deg = max_transition_angle
    
    # Transition dynamics
    # Horizontal acceleration during transition
    # a_h = g * tan(θ) where θ is angle from vertical
    # This is the horizontal component of acceleration
    max_horizontal_accel = 9.81 * math.tan(max_angle_rad)
    
    # Time to reach maximum angle
    # Assuming constant rotation rate
    rotation_rate = max_angle_rad / transition_time if transition_time > 0 else 0
    
    # G-forces during transition
    # Total acceleration = g + a_h (vector sum)
    # This creates additional structural loads
    total_acceleration = math.sqrt(9.81**2 + max_horizontal_accel**2)
    g_load = total_acceleration / 9.81
    
    # Safety assessment
    safe_transition = can_hover and can_sustain_at_max_angle
    
    return {
        "can_hover": can_hover,
        "hover_margin_percent": hover_margin * 100,
        "can_sustain_at_max_angle": can_sustain_at_max_angle,
        "max_safe_angle_deg": safe_angle_deg,
        "max_horizontal_accel_ms2": max_horizontal_accel,
        "g_load_during_transition": g_load,
        "safe_transition": safe_transition,
        "warning": "Transition unsafe" if not safe_transition else "Transition analysis complete"
    }

# Self-test: known safe case should pass
SELFTEST = {
    "params": {
        "aircraft_weight_N": 1000,
        "vertical_thrust_N": 1200,  # More than weight
        "max_transition_angle_deg": 30,  # Conservative angle
        "transition_time_s": 15,  # Slow transition
        "wind_speed_ms": 0
    },
    "expect": {
        "can_hover": True,  # Should be able to hover
        "safe_transition": True,  # Should be safe
        "g_load_during_transition": 1.15,  # Low g-load expected
    },
    "tol": 0.01
}