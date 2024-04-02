import math
from typing import Literal, Tuple

import torch
from torch import Tensor
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.typing import Adj
from torch_geometric.nn import MessagePassing
from torch_geometric.nn.aggr import MaxAggregation, SumAggregation
from torch_geometric.nn.inits import reset


class CMPNN(nn.Module):

    def __init__(
        self,
        in_channels: int,
        hidden_channels: int,
        out_channels: int,
        edge_dim: int,
        num_layers: int,
        communicator_name: str,
        dropout: float = 0.0,
    ) -> None:
        
        super().__init__()

        self.in_channels = in_channels
        self.hidden_channels = hidden_channels
        self.out_channels = out_channels
        self.edge_dim = edge_dim
        self.num_layers = num_layers
        self.communicator_name = communicator_name

        self.atom_proj = nn.Sequential(
            nn.Linear(in_channels, hidden_channels),
            nn.ReLU()
        )
        self.bond_proj = nn.Sequential(
            nn.Linear(edge_dim, hidden_channels),
            nn.ReLU()
        )
        
        self.convs = nn.ModuleList()
        for _ in range(self.num_layers): 
            self.convs.append(
                GCNEConv(hidden_channels, hidden_channels, communicator_name, dropout)
            )

        self.lin = nn.Linear(hidden_channels * 3, out_channels)
        self.gru = BatchGRU(hidden_channels)
        self.seq_out = nn.Sequential(
            nn.Linear(hidden_channels * 2, hidden_channels),
            nn.ReLU(),
            nn.Dropout(p=dropout)
        )

        self.reset_parameters()

    def reset_parameters(self):
        """Resets all learnable parameters of the module."""
        
        reset(self.atom_proj)
        reset(self.bond_proj)
        for conv in self.convs:
            conv.reset_parameters()
        self.lin.reset_parameters()
        reset(self.seq_out)

    def forward(
        self, 
        x: Tensor, 
        edge_index: Adj, 
        edge_attr: Tensor,
        batch: Tensor
    ) -> Tensor:
        
        x_proj = self.atom_proj(x)
        h_atom = x_proj.clone()
        h_bond = self.bond_proj(edge_attr)
        
        for layer in self.convs[:-1]:
            h_atom, h_bond = layer(x=h_atom, edge_attr=h_bond, edge_index=edge_index)

        # nth layer message passing
        aggr_message, _ = self.convs[-1](h_atom, h_bond, edge_index)
        # aggr_message: Messgage from incoming bonds
        # h_atom: Current atom's representation
        # x_proj: Atom's initial representation
        h_atom = self.lin(torch.cat([aggr_message, h_atom, x_proj], 1))
        h_atom = self.gru(h_atom, batch)
        return self.seq_out(h_atom)


class GCNEConv(MessagePassing):

    def __init__(
        self, 
        in_channels: int,
        out_channels: int,
        communicator_name: str, 
        dropout: float = 0.0
    ) -> None:
        
        super().__init__(
            aggr=[SumAggregation(), MaxAggregation()], 
            aggr_kwargs=dict(mode='message_booster'),
            flow="target_to_source"
        )
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.communicator_name = communicator_name
        self.communicator = NodeEdgeMessageCommunicator(
            name=communicator_name,
            hidden_channels=in_channels
        )
        self.seq = nn.Sequential(
            nn.Linear(in_channels, out_channels),
            nn.ReLU(),
            nn.Dropout(p=dropout)
        )

        self.reset_parameters()

    def reset_parameters(self) -> None:
        """Resets all learnable parameters of the module."""

        super().reset_parameters()
        reset(self.communicator)
        reset(self.seq)

    def forward(
        self, 
        x: Tensor, 
        edge_attr: Tensor, 
        edge_index: Adj
    ) -> Tuple[Tensor, Tensor]:

        x = self.propagate(
            edge_index, 
            x=x, 
            edge_attr=edge_attr, 
            # Aggregation is done on the `edge_attr` based on `edge_index_i`
            # So the first dimension is not always equal to num_atoms 
            # i.e., edge_index_i.unique().shape != x.size(0)
            # The output should be of shape (num_atoms x hidden_channels)
            # `x.size(0)` will get assigned to `dim_size` that is passed to the
            # `aggregate` method
            size=[x.size(0), None] 
        )
        edge_attr = self.edge_updater(edge_index, x=x, edge_attr=edge_attr)
        return x, edge_attr
    
    def message(self, edge_attr: Tensor) -> Tensor:

        return edge_attr
    
    def update(self, message: Tensor, x: Tensor) -> Tensor:

        return self.communicator(message, x)

    def edge_update(
        self, 
        x: Tensor, 
        edge_attr: Tensor, 
        edge_index_i: Tensor,
        edge_index_j: Tensor
    ) -> Tensor:
        
        # For example,
        # Atom_0  - [Bond_0] -> Atom_1
        # Atom_0 <- [Bond_1] -  Atom_1
        # Bond_0 = Atom_0 - Bond_1
        bond_embed = x[edge_index_i] - edge_attr[edge_index_j]
        return self.seq(bond_embed)

    def __repr__(self) -> str:
        return (
            f'{self.__class__.__name__}({self.in_channels}, {self.out_channels}, '
            f"communicator_name='{self.communicator_name}')"
        )


class NodeEdgeMessageCommunicator(nn.Module):

    def __init__(
        self, 
        name: Literal["additive", "inner_product", "gru", "mlp"], 
        hidden_channels: int
    ) -> None:
        
        super().__init__()
        assert name in ("additive", "inner_product", "gru", "mlp"), f"Invalid communicator '{name}'!"
        self.name = name
        self.hidden_channels = hidden_channels
        self.communicator = None

        if name == "gru":
            self.communicator = nn.GRUCell(hidden_channels, hidden_channels)
        elif name == "mlp":
            self.communicator = nn.Sequential(
                nn.Linear(hidden_channels * 2, hidden_channels),
                nn.ReLU()
            )
    
    def forward(self, message: Tensor, hidden_state: Tensor) -> Tensor:

        if self.name == "additive":
            out = hidden_state + message
        elif self.name == "inner_product":
            out = hidden_state * message
        elif self.name == "gru":
            out = self.communicator(hidden_state, message)
        elif self.name == "mlp":
            message = torch.cat((hidden_state, message), dim=1)
            out = self.communicator(message)
        return out

    def __repr__(self) -> str:
        return (
            f'{self.__class__.__name__}({self.hidden_channels}, '
            f"name='{self.name}')"
        )


class BatchGRU(nn.Module):

    def __init__(self, hidden_channels: int, num_layers: int = 1) -> None:
        
        super().__init__()
        self.hidden_channels = hidden_channels
        self.gru = nn.GRU(hidden_channels, hidden_channels, num_layers=num_layers, batch_first=True, bidirectional=True)
        self.bias = nn.Parameter(torch.Tensor(hidden_channels))

        self.reset_parameters()

    def reset_parameters(self):

        self.bias.data.uniform_(
            -1.0 / math.sqrt(self.hidden_channels), 
            1.0 / math.sqrt(self.hidden_channels)
        )
    
    def forward(self, h_atom: Tensor, batch: Tensor) -> Tensor:

        device = h_atom.device
        num_atoms = h_atom.shape[0]
        message = F.relu(h_atom + self.bias)
        unique_values, counts = torch.unique(batch, return_counts=True)
        dim_1 = unique_values.shape[0] # No. of mol graphs in the batch (aka batch size)
        dim_2 = counts.max().item() # Maximum no. of atoms in the batch
        dim_3 = self.hidden_channels
        
        messages = torch.zeros((dim_1, dim_2, dim_3), device=device)
        hidden_states = torch.zeros((2, dim_1, dim_3), device=device) # 2 -> bidirectional
        for i, value in enumerate(unique_values):
            indices = (batch == value).nonzero().squeeze(1)
            num_samples = counts[i]
            messages[i, :num_samples] = message[indices]
            hidden_states[:, i, :] = h_atom[indices].max(0)[0]
        
        h_messages, _ = self.gru(messages, hidden_states)

        unpadded_messages = torch.zeros((num_atoms, dim_3 * 2), device=device)
        for i, value in enumerate(unique_values):
            num_samples = counts[i]
            unpadded_messages[batch == value, :] = h_messages[i, :num_samples].view(-1, dim_3 * 2)

        return unpadded_messages