"""
Copyright (c) Meta, Inc. and its affiliates.
This source code is licensed under the MIT license found in the
LICENSE file in the root directory of this source tree.
"""

from __future__ import annotations

import math

import torch

from fairchem.core.modules.scaling import ScaleFactor

from .base_layers import Dense, ResidualLayer


class AtomUpdateBlock(torch.nn.Module):
    """
    Aggregate the message embeddings of the atoms

    Arguments
    ---------
    emb_size_atom: int
        Embedding size of the atoms.
    emb_size_edge: int
        Embedding size of the edges.
    emb_size_rbf: int
        Embedding size of the radial basis.
    nHidden: int
        Number of residual blocks.
    activation: callable/str
        Name of the activation function to use in the dense layers.
    """

    def __init__(
        self,
        emb_size_atom: int,
        emb_size_edge: int,
        emb_size_rbf: int,
        nHidden: int,
        activation=None,
    ) -> None:
        super().__init__()

        self.dense_rbf = Dense(emb_size_rbf, emb_size_edge, activation=None, bias=False)
        self.scale_sum = ScaleFactor()

        self.layers = self.get_mlp(emb_size_edge, emb_size_atom, nHidden, activation)

    def get_mlp(self, units_in: int, units: int, nHidden: int, activation):
        if units_in != units:
            dense1 = Dense(units_in, units, activation=activation, bias=False)
            mlp = [dense1]
        else:
            mlp = []
        res = [
            ResidualLayer(units, nLayers=2, activation=activation)
            for _ in range(nHidden)
        ]
        mlp += res
        return torch.nn.ModuleList(mlp)

    def forward(self, h: torch.Tensor, m, basis_rad, idx_atom):
        """
        Returns
        -------
        h: torch.Tensor, shape=(nAtoms, emb_size_atom)
            Atom embedding.
        """
        nAtoms = h.shape[0]

        bases_emb = self.dense_rbf(basis_rad)  # (nEdges, emb_size_edge)
        x = m * bases_emb

        x2 = torch.zeros(nAtoms, x.shape[1], device=x.device, dtype=x.dtype)
        x2.scatter_add_(0, idx_atom.unsqueeze(-1).expand(-1, x.shape[1]), x)
        x = self.scale_sum(x2, ref=m)

        for layer in self.layers:
            x = layer(x)  # (nAtoms, emb_size_atom)

        return x


class OutputBlock(AtomUpdateBlock):
    """
    Combines the atom update block and subsequent final dense layer.

    Arguments
    ---------
    emb_size_atom: int
        Embedding size of the atoms.
    emb_size_edge: int
        Embedding size of the edges.
    emb_size_rbf: int
        Embedding size of the radial basis.
    nHidden: int
        Number of residual blocks before adding the atom embedding.
    nHidden_afteratom: int
        Number of residual blocks after adding the atom embedding.
    activation: str
        Name of the activation function to use in the dense layers.
    direct_forces: bool
        If true directly predict forces, i.e. without taking the gradient
        of the energy potential.
    """

    def __init__(
        self,
        emb_size_atom: int,
        emb_size_edge: int,
        emb_size_rbf: int,
        nHidden: int,
        nHidden_afteratom: int,
        activation: str | None = None,
        direct_forces: bool = True,
    ) -> None:
        super().__init__(
            emb_size_atom=emb_size_atom,
            emb_size_edge=emb_size_edge,
            emb_size_rbf=emb_size_rbf,
            nHidden=nHidden,
            activation=activation,
        )

        self.direct_forces = direct_forces

        self.seq_energy_pre = self.layers  # inherited from parent class
        if nHidden_afteratom >= 1:
            self.seq_energy2 = self.get_mlp(
                emb_size_atom, emb_size_atom, nHidden_afteratom, activation
            )
            self.inv_sqrt_2 = 1 / math.sqrt(2.0)
        else:
            self.seq_energy2 = None

        if self.direct_forces:
            self.scale_rbf_F = ScaleFactor()
            self.seq_forces = self.get_mlp(
                emb_size_edge, emb_size_edge, nHidden, activation
            )
            self.dense_rbf_F = Dense(
                emb_size_rbf, emb_size_edge, activation=None, bias=False
            )

    def forward(self, h: torch.Tensor, m: torch.Tensor, basis_rad, idx_atom):
        """
        Returns
        -------
        torch.Tensor, shape=(nAtoms, emb_size_atom)
            Output atom embeddings.
        torch.Tensor, shape=(nEdges, emb_size_edge)
            Output edge embeddings.
        """
        nAtoms = h.shape[0]

        # ------------------------ Atom embeddings ------------------------ #
        basis_emb_E = self.dense_rbf(basis_rad)  # (nEdges, emb_size_edge)
        x = m * basis_emb_E

        x_E = torch.zeros(nAtoms, x.shape[1], device=x.device, dtype=x.dtype)
        x_E.scatter_add_(0, idx_atom.unsqueeze(-1).expand(-1, x.shape[1]), x)
        x_E = self.scale_sum(x_E, ref=m)

        for layer in self.seq_energy_pre:
            x_E = layer(x_E)  # (nAtoms, emb_size_atom)

        if self.seq_energy2 is not None:
            x_E = x_E + h
            x_E = x_E * self.inv_sqrt_2
            for layer in self.seq_energy2:
                x_E = layer(x_E)  # (nAtoms, emb_size_atom)

        # ------------------------- Edge embeddings ------------------------ #
        if self.direct_forces:
            x_F = m
            for _, layer in enumerate(self.seq_forces):
                x_F = layer(x_F)  # (nEdges, emb_size_edge)

            basis_emb_F = self.dense_rbf_F(basis_rad)
            # (nEdges, emb_size_edge)
            x_F_basis = x_F * basis_emb_F
            x_F = self.scale_rbf_F(x_F_basis, ref=x_F)
        else:
            x_F = 0
        # ------------------------------------------------------------------ #

        return x_E, x_F
