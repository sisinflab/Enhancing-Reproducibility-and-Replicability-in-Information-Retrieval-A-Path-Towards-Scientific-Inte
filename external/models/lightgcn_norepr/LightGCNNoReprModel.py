from abc import ABC

from torch_geometric.nn import LGConv
import torch
import torch_geometric
import numpy as np
import random


class LightGCNNoReprModel(torch.nn.Module, ABC):
    def __init__(self,
                 num_users,
                 num_items,
                 learning_rate,
                 embed_k,
                 l_w,
                 n_layers,
                 edge_index,
                 normalize,
                 random_seed,
                 name="LightGCNNoRepr",
                 **kwargs
                 ):
        super().__init__()

        # set seed
        random.seed(random_seed)
        np.random.seed(random_seed)
        torch.manual_seed(random_seed)
        torch.cuda.manual_seed(random_seed)
        torch.cuda.manual_seed_all(random_seed)
        # torch.backends.cudnn.deterministic = True
        # torch.use_deterministic_algorithms(True)

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        self.num_users = num_users
        self.num_items = num_items
        self.embed_k = embed_k
        self.learning_rate = learning_rate
        self.l_w = l_w
        self.n_layers = n_layers
        self.weight_size_list = [self.embed_k] * (self.n_layers + 1)
        self.alpha = torch.tensor([1 / (k + 1) for k in range(len(self.weight_size_list))])
        self.edge_index = edge_index
        self.normalize = normalize

        self.Gu = torch.nn.Embedding(
            num_embeddings=self.num_users, embedding_dim=self.embed_k)
        self.Gi = torch.nn.Embedding(
            num_embeddings=self.num_items, embedding_dim=self.embed_k)
        torch.nn.init.normal_(self.Gu.weight, std=0.1)
        torch.nn.init.normal_(self.Gi.weight, std=0.1)

        propagation_network_list = []

        for _ in range(self.n_layers):
            propagation_network_list.append((LGConv(normalize=self.normalize), 'x, edge_index -> x'))

        self.propagation_network = torch_geometric.nn.Sequential('x, edge_index', propagation_network_list)
        self.propagation_network.to(self.device)
        self.softplus = torch.nn.Softplus()

        self.optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)

    def propagate_embeddings(self, evaluate=False):
        ego_embeddings = torch.cat((self.Gu.weight.to(self.device), self.Gi.weight.to(self.device)), 0)
        all_embeddings = [ego_embeddings]

        for layer in range(0, self.n_layers):
            if evaluate:
                self.propagation_network.eval()
                with torch.no_grad():
                    all_embeddings += [list(
                        self.propagation_network.children()
                    )[layer](all_embeddings[layer].to(self.device), self.edge_index.to(self.device))]
            else:
                all_embeddings += [list(
                    self.propagation_network.children()
                )[layer](all_embeddings[layer].to(self.device), self.edge_index.to(self.device))]

        if evaluate:
            self.propagation_network.train()

        all_embeddings = torch.mean(torch.stack(all_embeddings, 0), dim=0)
        # all_embeddings = sum([all_embeddings[k] * self.alpha[k] for k in range(len(all_embeddings))])
        gu, gi = torch.split(all_embeddings, [self.num_users, self.num_items], 0)

        return gu, gi

    def forward(self, inputs, **kwargs):
        gu, gi = inputs
        gamma_u = torch.squeeze(gu).to(self.device)
        gamma_i = torch.squeeze(gi).to(self.device)

        xui = torch.sum(gamma_u * gamma_i, 1)

        return xui

    def predict(self, gu, gi, **kwargs):
        return torch.sigmoid(torch.matmul(gu.to(self.device), torch.transpose(gi.to(self.device), 0, 1)))

    def train_step(self, batch):
        gu, gi = self.propagate_embeddings()
        user, pos, neg = batch
        xu_pos = self.forward(inputs=(gu[user[:, 0]], gi[pos[:, 0]]))
        xu_neg = self.forward(inputs=(gu[user[:, 0]], gi[neg[:, 0]]))
        loss = torch.mean(torch.nn.functional.softplus(xu_neg - xu_pos))
        reg_loss = self.l_w * (1 / 2) * (self.Gu.weight[user[:, 0]].norm(2).pow(2) +
                                         self.Gi.weight[pos[:, 0]].norm(2).pow(2) +
                                         self.Gi.weight[neg[:, 0]].norm(2).pow(2)) / float(batch[0].shape[0])
        loss += reg_loss

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        return loss.detach().cpu().numpy()

    def get_top_k(self, preds, train_mask, k=100):
        return torch.topk(torch.where(torch.tensor(train_mask).to(self.device), preds.to(self.device),
                                      torch.tensor(-np.inf).to(self.device)), k=k, sorted=True)
