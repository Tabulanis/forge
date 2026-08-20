"""
Circular Wing Aerodynamics - Lift and Drag Properties
Analyzes the aerodynamic characteristics of a circular wing design.
"""

import math

META = {
    "name": "circular_wing_aerodynamics",
    "description": "Aerodynamic analysis of circular wing configurations",
    "why": "Need to understand lift/drag properties of non-conventional wing shapes"
}

def run(params):
    """
    params: dict with keys:
    - diameter_m: diameter of circular wing (m)
    - airspeed_ms: airspeed over wing (m/s)
    - air_density: air density (kg/m³, default 1.225)
    - angle_of_attack_deg: angle of attack in degrees (default 0)
    - camber_ratio: wing camber as fraction of chord (default 0.02)
    """
    
    diameter = params.get("diameter_m", 10)
    airspeed = params.get("airspeed_ms", 20)
    air_density = params.get("air_density", 1.225)
    angle_of_attack = params.get("angle_of_attack_deg", 0)
    camber_ratio = params.get("camber_ratio", 0.02)
    
    # Wing area
    wing_area = math.pi * (diameter/2)**2
    
    # Aspect ratio for circular wing
    # AR = b²/S where b is span (diameter for circular wing)
    aspect_ratio = diameter**2 / wing_area
    
    # Base lift coefficient (simplified airfoil theory)
    # Cl = 2π * α * (1 + camber_effect)
    # This is a simplified model - real circular wings would have different properties
    alpha_rad = math.radians(angle_of_attack)
    camber_effect = 1 + 2 * camber_ratio  # Simplified camber effect
    base_cl = 2 * math.pi * alpha_rad * camber_effect
    
    # Drag coefficient (parasite + induced)
    # Cd = Cd0 + Cl²/(π*AR*e)
    # e is Oswald efficiency factor (0.7-0.9 for conventional wings)
    # Circular wing would have different efficiency
    cd0 = 0.02  # Base parasite drag
    efficiency = 0.6  # Reduced efficiency for circular wing
    induced_drag = base_cl**2 / (math.pi * aspect_ratio * efficiency)
    cd = cd0 + induced_drag
    
    # Lift and drag forces
    lift = 0.5 * air_density * airspeed**2 * base_cl * wing_area
    drag = 0.5 * air_density * airspeed**2 * cd * wing_area
    
    # Lift-to-drag ratio
    ld_ratio = lift / drag if drag > 0 else float('inf')
    
    # Check for stall
    stall_angle = 15  # degrees - simplified
    stalled = abs(angle_of_attack) > stall_angle
    
    return {
        "wing_area_m2": wing_area,
        "aspect_ratio": aspect_ratio,
        "lift_coefficient": base_cl,
        "drag_coefficient": cd,
        "lift_force_N": lift,
        "drag_force_N": drag,
        "lift_drag_ratio": ld_ratio,
        "stalled": stalled,
        "warning": "Wing stalled" if stalled else "Aerodynamic analysis complete"
    }

# Self-test: check that basic aerodynamic relationships hold
SELFTEST = {
    "params": {
        "diameter_m": 10,
        "airspeed_ms": 20,
        "air_density": 1.225,
        "angle_of_attack_deg": 2,  # Small angle for linear region
        "camber_ratio": 0.02
    },
    "expect": {
        "wing_area_m2": 78.54,  # π * 25
        "aspect_ratio": 1.27,  # Should be low for circular wing
        "lift_drag_ratio": 5.47,  # Corrected - circular wings are inefficient
        "stalled": False  # Should not be stalled at 2 degrees
    },
    "tol": 0.01
}