from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Convert text to TF-IDF vectors
vectorizer = TfidfVectorizer()


def compute_cosine_similarity(text1, text2):
    tfidf_matrix = vectorizer.fit_transform([text1, text2])
    similarity = cosine_similarity(tfidf_matrix[0], tfidf_matrix[1])
    return float(similarity[0][0])
