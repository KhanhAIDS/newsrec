import torch
from torch import nn
import torch.nn.functional as F

from .model_utils import AttentionPooling


class NewsEncoder(nn.Module):
    def __init__(self, args, embedding_matrix, num_category, num_subcategory):
        super(NewsEncoder, self).__init__()
        self.embedding_matrix = embedding_matrix
        self.drop_rate = args.drop_rate
        self.num_words_title = args.num_words_title
        self.use_category = args.use_category
        self.use_subcategory = args.use_subcategory
        if args.use_category:
            self.category_emb = nn.Embedding(num_category + 1, args.category_emb_dim, padding_idx=0)
            self.category_dense = nn.Linear(args.category_emb_dim, args.news_dim)
        if args.use_subcategory:
            self.subcategory_emb = nn.Embedding(num_subcategory + 1, args.category_emb_dim, padding_idx=0)
            self.subcategory_dense = nn.Linear(args.category_emb_dim, args.news_dim)
        if args.use_category or args.use_subcategory:
            self.final_attn = AttentionPooling(args.news_dim, args.news_query_vector_dim)
        self.cnn = nn.Conv1d(
            in_channels=args.word_embedding_dim,
            out_channels=args.news_dim,
            kernel_size=3,
            padding=1
        )
        self.attn = AttentionPooling(args.news_dim, args.news_query_vector_dim)

    def forward(self, x, mask=None):
        '''
            x: batch_size, word_num
            mask: batch_size, word_num
        '''
        title = torch.narrow(x, -1, 0, self.num_words_title).long()
        word_vecs = F.dropout(self.embedding_matrix(title),
                              p=self.drop_rate,
                              training=self.training)
        context_word_vecs = self.cnn(word_vecs.transpose(1, 2)).transpose(1, 2)
        title_vecs = self.attn(context_word_vecs, mask)
        all_vecs = [title_vecs]

        start = self.num_words_title
        if self.use_category:
            category = torch.narrow(x, -1, start, 1).squeeze(dim=-1).long()
            category_vecs = self.category_dense(self.category_emb(category))
            all_vecs.append(category_vecs)
            start += 1
        if self.use_subcategory:
            subcategory = torch.narrow(x, -1, start, 1).squeeze(dim=-1).long()
            subcategory_vecs = self.subcategory_dense(self.subcategory_emb(subcategory))
            all_vecs.append(subcategory_vecs)

        if len(all_vecs) == 1:
            news_vecs = all_vecs[0]
        else:
            all_vecs = torch.stack(all_vecs, dim=1)
            news_vecs = self.final_attn(all_vecs)
        return news_vecs


class UserEncoder(nn.Module):
    def __init__(self, args):
        super(UserEncoder, self).__init__()
        self.args = args

        self.global_attn = AttentionPooling(args.news_dim, args.user_query_vector_dim)
        
        self.gru = nn.GRU(args.news_dim, args.news_dim, batch_first=True)
        
        self.layer_norm_gru = nn.LayerNorm(args.news_dim)

        self.alpha = nn.Parameter(torch.tensor(0.5))

        self.pad_doc = nn.Parameter(torch.empty(1, args.news_dim).uniform_(-1, 1)).type(torch.FloatTensor)

    def forward(self, news_vecs, log_mask=None):

        batch_size = news_vecs.size(0)


        if self.args.user_log_mask:
            long_term_vec = self.global_attn(news_vecs, log_mask)
        else:
            padding_doc = self.pad_doc.unsqueeze(dim=0).expand(batch_size, self.args.user_log_length, -1)
            news_vecs = news_vecs * log_mask.unsqueeze(dim=-1) + padding_doc * (1 - log_mask.unsqueeze(dim=-1))
            long_term_vec = self.global_attn(news_vecs)

        short_term_representations, _ = self.gru(news_vecs)
        
        short_term_representations = self.layer_norm_gru(short_term_representations)

        short_term_vec = short_term_representations[:, -1, :] 
        
        user_vec = self.alpha * long_term_vec + (1 - self.alpha) * short_term_vec

        return user_vec


class Model(torch.nn.Module):
    def __init__(self, args, embedding_matrix, num_category, num_subcategory, **kwargs):
        super(Model, self).__init__()
        self.args = args
        pretrained_word_embedding = torch.from_numpy(embedding_matrix).float()
        word_embedding = nn.Embedding.from_pretrained(pretrained_word_embedding,
                                                      freeze=args.freeze_embedding,
                                                      padding_idx=0)

        self.news_encoder = NewsEncoder(args, word_embedding, num_category, num_subcategory)
        self.user_encoder = UserEncoder(args)
        self.loss_fn = nn.CrossEntropyLoss()

    def forward(self, history, history_mask, candidate, label):
        '''
            history: batch_size, history_length, num_word_title
            history_mask: batch_size, history_length
            candidate: batch_size, 1+K, num_word_title
            label: batch_size, 1+K
        '''
        num_words = history.shape[-1]
        candidate_news = candidate.reshape(-1, num_words)
        candidate_news_vecs = self.news_encoder(candidate_news).reshape(-1, 1 + self.args.npratio, self.args.news_dim)

        history_news = history.reshape(-1, num_words)
        history_news_vecs = self.news_encoder(history_news).reshape(-1, self.args.user_log_length, self.args.news_dim)

        user_vec = self.user_encoder(history_news_vecs, history_mask)
        score = torch.bmm(candidate_news_vecs, user_vec.unsqueeze(dim=-1)).squeeze(dim=-1)
        loss = self.loss_fn(score, label)
        return loss, score
