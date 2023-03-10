import os
import torch
import torch.nn.functional as F
import numpy as np
from torch_geometric.loader import DataLoader
from torch_geometric.datasets import TUDataset
from sklearn.model_selection import KFold, train_test_split
from dig.auggraph.method.GraphAug.model import GIN, GCN
from dig.auggraph.datasets.aug_dataset import DegreeTrans, Subset, AUG_trans
from dig.auggraph.method.GraphAug.aug import Augmenter
from dig.auggraph.method.GraphAug.constants import *


class RunnerAugCls(object):
    def __init__(self, data_root_path, dataset_name, conf):
        self.conf = conf
        self.dataset = self._get_dataset(data_root_path, dataset_name)
        self.augmenter = self._get_aug_model()
        self.model = self._get_model()
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.train_data_trans = AUG_trans(self.augmenter, self.device, pre_trans=self.data_trans,
                                          post_trans=self.data_trans)
        self.dataset_name = dataset_name

    def _get_dataset(self, data_root_path, dataset_name):
        dataset = TUDataset(data_root_path, name=dataset_name.value)
        if dataset_name in [DatasetName.MUTAG]:
            self.data_trans = None
            self.conf[IN_DIMENSION] = dataset[0].x.shape[1]
            self.conf[EDGE_IN_DIMENSION] = dataset[0].x.shape[1]
        if dataset_name in [DatasetName.NCI1, DatasetName.NCI109, DatasetName.PROTEINS]:
            self.data_trans = None
            self.conf[IN_DIMENSION] = dataset[0].x.shape[1]
        elif dataset_name in [DatasetName.COLLAB, DatasetName.IMDB_BINARY]:
            self.data_trans = DegreeTrans(dataset)
            self.conf[IN_DIMENSION] = self.data_trans(dataset[0]).x.shape[1]
        self.conf[NUM_CLASSES] = dataset.num_classes
        return dataset

    def _get_aug_model(self):
        in_dim = self.conf[IN_DIMENSION]
        self.conf[GENERATOR_PARAMS][IN_DIMENSION] = in_dim
        if AugType.NODE_FM.value in self.conf[GENERATOR_PARAMS][AUG_TYPE_PARAMS]:
            self.conf[GENERATOR_PARAMS][AUG_TYPE_PARAMS][AugType.NODE_FM.value][NODE_FEAT_DIM] = in_dim
            assert(self.conf[GENERATOR_PARAMS][AUG_TYPE_PARAMS][AugType.NODE_FM.value][NODE_FEAT_DIM] == in_dim) # TODO
        else:
            assert(False) # TODO
        augmenter = Augmenter(**self.conf[GENERATOR_PARAMS])
        if self.conf[AUG_MODEL_PATH] is not None:
            augmenter.load_state_dict(torch.load(self.conf[AUG_MODEL_PATH], map_location=torch.device('cpu')))
        augmenter.eval()
        return augmenter

    def _get_model(self):
        if self.conf[MODEL_NAME] == CLSModelType.GIN:
            return GIN(self.conf[IN_DIMENSION], self.conf[NUM_CLASSES], self.conf[NUM_LAYERS],
                       self.conf[HIDDEN_UNITS], self.conf[DROPOUT])
        elif self.conf[MODEL_NAME] == CLSModelType.GCN:
            return GCN(self.conf[IN_DIMENSION], self.conf[NUM_CLASSES], self.conf[NUM_LAYERS], self.conf[HIDDEN_UNITS],
                       self.conf[DROPOUT])

    def _train_epoch(self, loader, optimizer):
        self.model.train()
        for data_batch in loader:
            data_batch = data_batch.to(self.device)
            optimizer.zero_grad()
            try:
                output = self.model(data_batch)
            except:
                print(data_batch.x.shape, data_batch.edge_index.shape)
                print(data_batch.batch)
                exit()
            loss = F.nll_loss(output, data_batch.y)
            loss.backward()
            optimizer.step()

    def test(self, loader):
        self.model.eval()
        num_correct = 0
        for data_batch in loader:
            data_batch = data_batch.to(self.device)
            output = self.model(data_batch)
            pred = output.max(dim=1)[1]
            num_correct += pred.eq(data_batch.y).sum().item()
        return num_correct / len(loader.dataset)

    def train_test(self, out_root_path, file_name='record.txt'):
        val_accs, test_accs = [], []
        kf = KFold(n_splits=10, shuffle=True)
        self.dataset.shuffle()
        self.model = self.model.to(self.device)

        out_path = os.path.join(out_root_path, self.dataset_name.value)
        if not os.path.isdir(out_path):
            os.makedirs(out_path)

        f = open(os.path.join(out_path, file_name), 'a')
        f.write('10-CV results for dataset {} with model {}, num layers {}, hidden {}\n'.format(self.dataset_name,
                                                                                                self.conf[MODEL_NAME].value,
                                                                                                self.conf[NUM_LAYERS],
                                                                                                self.conf[HIDDEN_UNITS]))
        f.write('Use the learnable augmentation with params below\n')
        for aug_type in self.conf[GENERATOR_PARAMS][AUG_TYPE_PARAMS]:
            f.write('{}: {}\n'.format(aug_type, self.conf[GENERATOR_PARAMS][AUG_TYPE_PARAMS][aug_type]))
        f.close()

        for i, (train_idx, test_idx) in enumerate(kf.split(list(range(len(self.dataset))))):
            train_idx, val_idx = train_test_split(train_idx, test_size=0.1)
            train_set, val_set, test_set = Subset(self.dataset[train_idx.tolist()], transform=self.train_data_trans), \
                Subset(self.dataset[val_idx.tolist()], transform=self.data_trans), Subset(
                self.dataset[test_idx.tolist()], transform=self.data_trans)
            train_loader = DataLoader(train_set, batch_size=self.conf[BATCH_SIZE], shuffle=True, num_workers=16)
            val_loader = DataLoader(val_set, batch_size=self.conf[BATCH_SIZE], shuffle=True)
            test_loader = DataLoader(test_set, batch_size=self.conf[BATCH_SIZE], shuffle=True)

            self.model.reset_parameters()
            optimizer = torch.optim.Adam(self.model.parameters(), lr=self.conf[INITIAL_LR])
            scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min',
                                                                   factor=self.conf[FACTOR],
                                                                   patience=self.conf[PATIENCE],
                                                                   min_lr=self.conf[MIN_LR])

            best_val_acc, best_test_acc = 0.0, 0.0
            for epoch in range(self.conf[MAX_NUM_EPOCHS]):
                lr = scheduler.optimizer.param_groups[0]['lr']
                self._train_epoch(train_loader, optimizer)

                val_acc = self.test(val_loader)
                print('Epoch {}, validation accuracy {}'.format(epoch, val_acc))

                test_acc = self.test(test_loader)

                scheduler.step(val_acc)

                if val_acc > best_val_acc:
                    best_val_acc = val_acc

                if test_acc > best_test_acc:
                    best_test_acc = test_acc

                if lr < self.conf[MIN_LR]:
                    break

            val_accs.append(best_val_acc)
            test_accs.append(best_test_acc)

            f = open(os.path.join(out_path, file_name), 'a')
            f.write('Split {}, validation accuracy {}, test accuracy {}\n'.format(i, best_val_acc, best_test_acc))
            f.close()

        f = open(os.path.join(out_path, file_name), 'a')
        f.write('Validation accuracy mean {}, std {}\n'.format(np.mean(val_accs), np.std(val_accs)))
        f.write('Test accuracy mean {}, std {}\n'.format(np.mean(test_accs), np.std(test_accs)))
        f.close()
