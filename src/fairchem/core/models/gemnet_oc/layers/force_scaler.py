"""
Copyright (c) Meta, Inc. and its affiliates.
This source code is licensed under the MIT license found in the
LICENSE file in the root directory of this source tree.
"""

from __future__ import annotations

import logging

import torch


class ForceStressScaler:
    """
    Scales up the energy and then scales down the forces/stresses
    to prevent NaNs and infs in calculations using AMP.
    Inspired by torch.cuda.amp.GradScaler.
    """

    def __init__(
        self,
        init_scale: float = 2.0**8,
        growth_factor: float = 2.0,
        backoff_factor: float = 0.5,
        growth_interval: int = 2000,
        max_force_iters: int = 50,
        enabled: bool = True,
    ) -> None:
        self.scale_factor = init_scale
        self.growth_factor = growth_factor
        self.backoff_factor = backoff_factor
        self.growth_interval = growth_interval
        self.max_force_iters = max_force_iters
        self.enabled = enabled
        self.finite_force_results = 0

    def scale(self, energy):
        return energy * self.scale_factor if self.enabled else energy

    def unscale(self, forces):
        return forces / self.scale_factor if self.enabled else forces

    def calc_forces(self, energy, pos):
        """Calculate forces only"""
        energy_scaled = self.scale(energy)
        forces_scaled = -torch.autograd.grad(
            energy_scaled,
            pos,
            grad_outputs=torch.ones_like(energy_scaled),
            create_graph=True,
        )[0]
        # (nAtoms, 3)
        return self.unscale(forces_scaled)

    def calc_forces_and_update(self, energy, pos):
        """Calculate forces with scaling update"""
        if self.enabled:
            found_nans_or_infs = True
            force_iters = 0

            # Re-calculate forces until everything is nice and finite.
            while found_nans_or_infs:
                forces = self.calc_forces(energy, pos)

                found_nans_or_infs = not torch.all(forces.isfinite())
                if found_nans_or_infs:
                    self.finite_force_results = 0

                    # Prevent infinite loop
                    force_iters += 1
                    if force_iters == self.max_force_iters:
                        logging.warning(
                            "Too many non-finite force results in a batch. "
                            "Breaking scaling loop."
                        )
                        break

                    # Delete graph to save memory
                    del forces
                else:
                    self.finite_force_results += 1
                self.update()
        else:
            forces = self.calc_forces(energy, pos)
        return forces

    def calc_forces_and_stresses(self, energy, pos, displacement, training=None):
        """Calculate both forces and stresses"""
        energy_scaled = self.scale(energy)

        forces_scaled, virials_scaled = torch.autograd.grad(
            [energy_scaled],
            [pos, displacement],
            grad_outputs=[torch.ones_like(energy_scaled)],
            create_graph=True,
            allow_unused=True,
        )

        # (nAtoms, 3)
        return -self.unscale(forces_scaled), self.unscale(virials_scaled)

    def calc_and_update(self, energy, pos, displacement, training=None):
        """Calculate forces and stresses with scaling update"""
        if self.enabled:
            found_nans_or_infs = True
            force_iters = 0

            # Re-calculate forces until everything is nice and finite.
            while found_nans_or_infs:
                forces, virials = self.calc_forces_and_stresses(
                    energy, pos, displacement, training
                )

                found_nans_or_infs = not (
                    torch.all(forces.isfinite()) and torch.all(virials.isfinite())
                )
                if found_nans_or_infs:
                    self.finite_force_results = 0

                    # Prevent infinite loop
                    force_iters += 1
                    if force_iters == self.max_force_iters:
                        logging.warning(
                            "Too many non-finite force/virial results in a batch. "
                            "Breaking scaling loop."
                        )
                        break

                    # Delete graph to save memory
                    del forces
                    del virials
                else:
                    self.finite_force_results += 1
                self.update()
        else:
            forces, virials = self.calc_forces_and_stresses(
                energy, pos, displacement, training
            )
        return forces, virials

    def update(self) -> None:
        if self.finite_force_results == 0:
            self.scale_factor *= self.backoff_factor

        if self.finite_force_results == self.growth_interval:
            self.scale_factor *= self.growth_factor
            self.finite_force_results = 0

        logging.info(f"finite force step count: {self.finite_force_results}")
        logging.info(f"scaling factor: {self.scale_factor}")
