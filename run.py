import argparse
import os, random
from sklearn.metrics.pairwise import cosine_similarity
import torch
import wandb
import yaml
from tqdm import tqdm
import utils
from model import SkipGram, NegativeSamplingLoss
from torch.optim import Adam
import eval.evaluation as evaluation
from pathlib import Path

def parse_args():
    """Parse input arguments"""

    parser = argparse.ArgumentParser(description='Experiment Args')

    parser.add_argument(
        '--RUN_MODE', dest='RUN_MODE',
        choices=['train', 'val'],
        help='{train, val}',
        type=str, required=True
    )

    parser.add_argument(
        '--CPU', dest='CPU',
        help='use CPU instead of GPU',
        action='store_true'
    )

    parser.add_argument(
        '--DEBUG', dest='DEBUG',
        help='enter debug mode',
        action='store_true'
    )

    parser.add_argument(
        '--SUBSAMPLING', dest='SUBSAMPLING',
        help='add subsampling to words',
        action='store_true'
    )
    parser.add_argument(
        '--VERSION', dest='VERSION',
        help='model version',
        type=int
    )
    parser.add_argument(
        '--CKPT_E', dest='CKPT_EPOCH',
        help='checkpoint epoch',
        type=int
    )
    parser.add_argument(
        '--NGRAMS', dest='NGRAMS',
        help='adding ngrams to tokens',
        action='store_true'
    )
    parser.add_argument(
        '--RESUME', dest='RESUME',
        help='resume training',
        action='store_true'
    )
    parser.add_argument(
        '--DATA', dest='DATA',
        help='location of dataset',
        type=str
    )
    args = parser.parse_args()
    return args


class MainExec(object):
    def __init__(self, args, config):
        self.args = args
        self.cfgs = config
        self.loss = None
        self.batch_loss = None
        self.subsampling = True if self.args.SUBSAMPLING else False
        self.ngrams = True if self.args.NGRAMS else False

        if self.args.CPU:
            self.device = torch.device("cpu")
        else:
            self.device = torch.device(
                "cuda" if torch.cuda.is_available() else "cpu"
            )
        if self.args.VERSION is None:
            self.model_ver = str(random.randint(0, 99999999)) # str(self.cfgs['NUM_MERGES'])
        else:
            self.model_ver = str(self.args.VERSION)

        print("Model version:", self.model_ver)

        # Fix seed
        self.seed = int(self.model_ver)
        torch.manual_seed(self.seed)
        torch.cuda.manual_seed_all(self.seed)
        random.seed(self.seed)

    def train(self):
        #wandb.init(entity='we', project='regular')
        dataset = utils.Dataset(self.args, self.cfgs)
        tokens = dataset.get_tokens()
        vocab = dataset.get_vocab_cls()
        pad_index = vocab.add_token('PAD</w>')
        vocab_size = len(vocab)
        ng_dist = utils.get_noise_dist(tokens)
        dataloader = utils.DataLoader(dataset, self.cfgs, NGRAMS=self.args.NGRAMS)
        data_size = len(tokens)
        print('Total data instances: ', data_size)
        model = SkipGram(self.cfgs, vocab_size, ng_dist=ng_dist, NGRAMS=self.args.NGRAMS, n_max=dataset.n_max,
                         pad_index=pad_index).to(self.device)
        loss_func = NegativeSamplingLoss(model, self.cfgs).to(self.device)
        optimizer = Adam(model.parameters(), lr=self.cfgs['LEARNING_RATE'])

        if self.args.RESUME:
            print('Resume training...')
            start_epoch = self.args.CKPT_EPOCH
            print('Loading Model ...')
            path = os.path.join(os.getcwd(), 'models',
                                self.model_ver,
                                'epoch' + str(start_epoch) + '.pkl')

            # Load state dict of the model and optimizer
            ckpt = torch.load(path, map_location=self.device)
            model.load_state_dict(ckpt['state_dict'])
            optimizer.load_state_dict(ckpt['optimizer'])
        else:
            # This block of code is usually executed when training is first started
            start_epoch = 0
            Path(os.path.join(os.getcwd(), 'models')).mkdir(parents=True, exist_ok=True)
            os.mkdir(os.path.join(os.getcwd(), 'models', self.model_ver))

        model.train()
        print('Training started ...')
        print_every = 20
        for epoch in range(start_epoch, self.cfgs['EPOCHS']):
            loss_sum = 0
            with tqdm(dataloader.get_batches()) as tepoch:
                for step, (
                        input_words, target_words
                ) in enumerate(tepoch):

                    tepoch.set_description("Epoch {}".format(str(epoch)))
                    if self.args.NGRAMS:
                        # In this case input_words are in form of list of lists, so using torch.cat we flatten list of
                        # lists to single tensor which can be used to get embeddings. Each list contains indices of
                        # word and its ngrams
                        inputs = torch.cat(input_words).to(self.device)
                        targets = torch.tensor(target_words).to(self.device)
                    else:
                        inputs, targets = torch.LongTensor(input_words), torch.LongTensor(target_words)
                        inputs, targets = inputs.to(self.device), targets.to(self.device)
                    optimizer.zero_grad()
                    loss = loss_func(inputs, targets)
                    loss.backward()
                    optimizer.step()
                    loss_sum += loss.item()
                    tepoch.set_postfix(loss=loss.item())

            evaluation.show_learning(model.out_embeddings, vocab, self.device)
            #similarity_metric = evaluation.get_similarity_metric(model.out_embeddings, vocab)
            self.loss = loss_sum/data_size
            self.batch_loss = loss_sum

            #wandb.log({'batch_loss': self.batch_loss, 'loss': self.loss, 'sim_metric': similarity_metric})
            if epoch % print_every == 0:
                self.eval(vocab, model.out_embeddings)
            epoch_finish = epoch + 1
            # Save checkpoint
            state = {
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'embeds':model.out_embeddings,
                'vocab':vocab
            }

            torch.save(
                state,
                os.path.join(os.getcwd(), 'models',
                             self.model_ver,
                             'epoch' + str(epoch_finish) + '.pkl')
            )

    def eval(self, vocab_ins=None, embeds=None):
        """
        This method evaluates trained embeddings based on some standard evaluation tasks like similarity between words
        and word analogy. It can either be called from command line or from some other method. It needs created vocab
        instance as well as trained embedding when called from another method. Otherwise, it retrieves required info
        from model's saved data.
        :param vocab_ins:
        :param embeds:
        :return:
        """
        if self.args.RUN_MODE == 'val':
            if self.args.CKPT_EPOCH is not None:
                path = os.path.join(os.getcwd(), 'models',
                                    self.model_ver,
                                    'epoch' + str(self.args.CKPT_EPOCH) + '.pkl')
                # Load state dict of the model
                ckpt = torch.load(path, map_location=self.device)
                embeddings = ckpt['embeds']
                vocab = ckpt['vocab']
            else:
                print('CHECKPOINT not provided')
                exit(-1)
        else:
            vocab = vocab_ins
            embeddings = embeds
        evaluation.semantic_similarity_datasets(embeddings, vocab)



    def overfit(self):
        dataset = utils.Dataset(self.args, self.cfgs)
        tokens = dataset.get_tokens()
        vocab = dataset.get_vocab_cls()
        pad_index = vocab.add_token('PAD</w>')
        vocab_size = len(vocab)
        ng_dist = utils.get_noise_dist(tokens)
        dataloader = utils.DataLoader(dataset, self.cfgs, NGRAMS=self.args.NGRAMS)

        model = SkipGram(self.cfgs, vocab_size, ng_dist=ng_dist, NGRAMS=self.args.NGRAMS, n_max=dataset.n_max,
                         pad_index=pad_index).to(self.device)
        loss_func = NegativeSamplingLoss(model, self.cfgs).to(self.device)
        optimizer = Adam(model.parameters(), lr=self.cfgs['LEARNING_RATE'])

        model.train()
        input_words, target_words = next(iter(dataloader.get_batches()))
        if self.args.NGRAMS:
            inputs = torch.cat(input_words).to(self.device)
            targets = torch.tensor(target_words).to(self.device)
        else:
            inputs, targets = torch.LongTensor(input_words), torch.LongTensor(target_words)
            inputs, targets = inputs.to(self.device), targets.to(self.device)

        for epoch in range(self.cfgs['EPOCHS']):
            optimizer.zero_grad()
            loss = loss_func(inputs, targets)
            loss.backward()
            optimizer.step()
            print('epoch {}, loss {}'.format(epoch, round(loss.item(), 3)))

    def run(self, run_mode):
        if run_mode == 'train' and self.args.DEBUG:
            print('Overfitting a single batch...')
            self.overfit()
        elif run_mode == 'train':
            print('Starting training mode...')
            self.train()
        elif run_mode == 'val':
            print('Starting validation mode...')
            self.eval()
        else:
            exit(-1)


if __name__ == "__main__":
    args = parse_args()

    with open('./config.yml', 'r') as f:
        config = yaml.safe_load(f)

    main_inst = MainExec(args, config)
    main_inst.run(args.RUN_MODE)
