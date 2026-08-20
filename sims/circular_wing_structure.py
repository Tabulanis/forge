"""
Circular Wing Structural Integrity - Load Analysis
Checks if a circular wing structure can handle aerodynamic loads.
"""

import math

META = {
    "name": "circular_wing_structure",
    "description": "Structural load analysis for circular wing designs",
    "why": "Need to know if circular wing can survive aerodynamic forces"
}

def run(params):
    """
    params: dict with keys:
    - diameter_m: wing diameter (m)
    - thickness_m: wing structural thickness (m)
    - material_yield_stress_Pa: yield stress of material (Pa)
    - lift_force_N: total lift force from aerodynamics (N)
    - safety_factor: required safety factor (default 1.5)
    """
    
    diameter = params.get("diameter_m", 10)
    thickness = params.get("thickness_m", 0.1)
    yield_stress = params.get("material_yield_stress_Pa", 350e6)  # Aluminum typical
    lift_force = params.get("lift_force_N", 0)
    safety_factor = params.get("safety_factor", 1.5)
    
    # Model wing as a beam fixed at center, loaded uniformly
    # Maximum bending moment for uniformly loaded beam: M = w*L²/8
    # where w is load per unit length, L is span
    # For circular wing, this is more complex - using simplified model
    
    # Total bending moment (conservative estimate)
    # Assuming lift distributed across wing, maximum stress at root
    radius = diameter / 2
    # Simplified: treat as cantilever with distributed load
    # M_max = F * r / 4 (conservative)
    max_moment = lift_force * radius / 4
    
    # Section modulus for rectangular cross-section
    # S = b*t²/6 where b is width, t is thickness
    # For circular wing segment, approximate as rectangular
    # Width of segment at root ≈ circumference/number_of_segments
    # Simplified: use full circumference as width
    width = math.pi * diameter  # Full circumference
    section_modulus = width * thickness**2 / 6
    
    # Maximum bending stress
    max_stress = max_moment / section_modulus if section_modulus > 0 else 0
    
    # Check against yield stress with safety factor
    allowable_stress = yield_stress / safety_factor
    structurally_sound = max_stress <= allowable_stress
    
    # Stress ratio (how close to failure)
    stress_ratio = max_stress / yield_stress if yield_stress > 0 else 0
    
    return {
        "max_bending_moment_Nm": max_moment,
        "section_modulus_m3": section_modulus,
        "max_stress_Pa": max_stress,
        "allowable_stress_Pa": allowable_stress,
        "structurally_sound": structurally_sound,
        "stress_ratio": stress_ratio,
        "warning": "Structural failure likely" if not structurally_sound else "Structural analysis complete"
    }

# Self-test: known case - small load should pass easily
SELFTEST = {
    "params": {
        "diameter_m": 1,
        "thickness_m": 0.01,
        "material_yield_stress_Pa": 350e6,
        "lift_force_N": 100,  # Small load
        "safety_factor": 1.5
    },
    "expect": {
        "structurally_sound": True,  # Small load should be fine
        "stress_ratio": 0.00068,  # Corrected value
    },
    "tol": 0.01
}