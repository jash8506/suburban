class Battery:
    def __init__(self, capacity_j, max_charge_w, max_discharge_w, efficiency):
        self.capacity_j = capacity_j
        self.max_charge_w = max_charge_w
        self.max_discharge_w = max_discharge_w
        self.efficiency = efficiency
        self.soc = capacity_j / 2  # State of Charge in wh

    def charge(self, power_w, s):
        if self.soc >= self.capacity_j:
            return 0
        # Limit charge power by max charge power
        power_w = min(power_w, self.max_charge_w)
        energy_added = power_w * s * self.efficiency
        self.soc = self.soc + energy_added
        return energy_added

    def discharge(self, power_w, s):
        if self.soc <= 0:
            return 0
        # Limit discharge power by max discharge power
        power_w = min(power_w, self.max_discharge_w)
        energy_removed = power_w * s
        self.soc = self.soc - energy_removed
        return energy_removed
