"""
VTOL Circular Wing Aircraft - Basic Physics Check
Checks if a circular wing design can transition from vertical to forward flight.
"""

import math

META = {
    "name": "vtol_circular_wing",
    "description": "Physics check for VTOL aircraft with circular wing",
    "why": "Need to understand if circular wing VTOL is physically possible and what constraints exist"
}

def run(params):
    """
    params: dict with keys:
    - mass_kg: aircraft mass (kg)
    - wing_diameter_m: diameter of circular wing (m)
    - air_density: air density (kg/m³, default 1.225 for sea level)
    - max_thrust_factor: ratio of max thrust to weight (default 1.5 for safety)
    - transition_speed: speed for transition to forward flight (m/s, default 20)
    """
    
    mass = params.get("mass_kg", 1000)
    diameter = params.get("wing_diameter_m", 10)
    air_density = params.get("air_density", 1.225)
    max_thrust_factor = params.get("max_thrust_factor", 1.5)
    transition_speed = params.get("transition_speed", 20)
    
    # Calculate wing area (circle)
    wing_area = math.pi * (diameter/2)**2
    
    # Weight
    weight = mass * 9.81
    
    # Thrust requirements for vertical takeoff
    required_thrust = weight * max_thrust_factor
    
    # Lift equation: L = 0.5 * rho * v² * Cl * A
    # For transition, we need lift >= weight at transition speed
    # Solve for required Cl: Cl = 2 * weight / (rho * v² * A)
    required_cl = 2 * weight / (air_density * transition_speed**2 * wing_area)
    
    # Check if this is physically possible
    # Typical max Cl for airfoils is 1.5-2.0, maybe 2.5 with flaps
    # Circular wing would have different characteristics
    max_realistic_cl = 1.8  # Conservative estimate
    
    possible = required_cl <= max_realistic_cl
    
    # Calculate power requirements
    # Thrust power = thrust * velocity for vertical climb
    vertical_climb_rate = (required_thrust - weight) / mass  # m/s
    power_required = required_thrust * vertical_climb_rate
    
    return {
        "wing_area_m2": wing_area,
        "required_thrust_N": required_thrust,
        "required_cl": required_cl,
        "max_realistic_cl": max_realistic_cl,
        "transition_possible": possible,
        "vertical_climb_rate_ms": vertical_climb_rate,
        "power_required_kw": power_required / 1000,
        "warning": "Required Cl exceeds realistic limits" if not possible else "Physics check passed"
    }

# Self-test with a known case: a small drone that should work
SELFTEST = {
    "params": {
        "mass_kg": 5,
        "wing_diameter_m": 1,
        "air_density": 1.225,
        "max_thrust_factor": 2.0,
        "transition_speed": 10
    },
    "expect": {
        "transition_possible": True,  # Small drone should be possible
        "wing_area_m2": 0.7854,  # π * 0.25
        "required_cl": 1.02,  # Corrected value
        "vertical_climb_rate_ms": 9.81,  # Should be positive
        "power_required_kw": 0.9624  # Corrected value
    },
    "tol": 0.01
}